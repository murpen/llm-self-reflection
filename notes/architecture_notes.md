# Structured Output Schema & Architecture Notes

## Response Schema

```json
{
  "action": {
    "type": "string",
    "enum": ["DISCUSS", "PROPOSE", "REVISE", "ACCEPT"],
    "required": true,
    "description": "The protocol action for this turn"
  },
  "discussion": {
    "type": "string",
    "required": true,
    "description": "The model's reasoning, observations, questions, challenges, or rationale. Always populated regardless of action type."
  },
  "statement": {
    "type": "string",
    "required": false,
    "description": "A candidate or endorsed consensus statement. Required for PROPOSE and REVISE. Optional for DISCUSS (to float preliminary ideas). Optional for ACCEPT (to echo the accepted statement for clarity)."
  }
}
```

## How Each Action Uses the Fields

| Action  | discussion                                      | statement                                      |
|---------|------------------------------------------------|-------------------------------------------------|
| DISCUSS | Main content: exploration, questions, challenges| Optional: preliminary ideas not yet proposed    |
| PROPOSE | Rationale for why this statement is proposed    | Required: the full candidate consensus text     |
| REVISE  | What was changed and why                        | Required: the complete revised statement in full |
| ACCEPT  | Brief commentary on why this is accepted        | Optional: echo of accepted statement            |

### Key Design Decisions

1. **`discussion` is always required** — even an ACCEPT should explain WHY, which gives you data on what each model finds compelling about the proposal.

2. **REVISE requires the complete revised statement**, not a diff. This avoids ambiguity about what the current proposal actually is, and makes it trivial for the harness to track the current live proposal at any point.

3. **`statement` is optional on DISCUSS** — this lets models float trial-balloon ideas without the formal weight of a PROPOSE. You might see interesting patterns where models put something in `statement` during DISCUSS that later becomes a PROPOSE, either from them or adopted by another participant.

4. **No separate field for "quoted part being revised"** — keeping it simpler. The `discussion` field on a REVISE should explain what changed, which is sufficient for analysis. Adding a `revised_section` field would make the schema more complex without much benefit since you can diff programmatically.

---

## Conversation History Construction (Option 2)

### For each model's turn, build the messages array as follows:

```python
def build_messages(
    participant_id: int,
    transcript: list[dict],  # [{"participant": int, "response": dict, "round": int}]
    system_prompt: str,
) -> list[dict]:
    """
    Build the messages array for a given participant's turn.
    
    The current participant's previous turns go in 'assistant' role.
    All other participants' turns go in 'user' role.
    Messages are in chronological order.
    """
    messages = [{"role": "system", "content": system_prompt}]
    
    for entry in transcript:
        # Format the response for display
        content = format_turn(entry)
        
        if entry["participant"] == participant_id:
            messages.append({"role": "assistant", "content": content})
        else:
            messages.append({"role": "user", "content": content})
    
    # Final user message to prompt the model's next turn
    messages.append({
        "role": "user",
        "content": f"It is now your turn (Participant {participant_id}). Respond following the protocol."
    })
    
    return messages


def format_turn(entry: dict) -> str:
    """
    Format a transcript entry for inclusion in messages.
    Uses anonymous participant numbers only — no model names.
    """
    p = entry["participant"]
    r = entry["response"]  # The parsed structured output
    action = r["action"]
    
    parts = [f"[Participant {p}]"]
    parts.append(f"Action: {action}")
    
    if r.get("discussion"):
        parts.append(f"Discussion: {r['discussion']}")
    
    if r.get("statement"):
        parts.append(f"Statement: {r['statement']}")
    
    return "\n".join(parts)
```

### Handling the user/assistant alternation problem

The OpenAI-style API (which OpenRouter follows) typically requires alternating
user/assistant messages. When multiple other participants speak in sequence,
you'll have consecutive user messages. Options:

**Option A: Merge consecutive same-role messages**
```python
# If the last message was 'user' and the next is also 'user', 
# merge them with a separator
if messages[-1]["role"] == "user" and new_role == "user":
    messages[-1]["content"] += f"\n\n---\n\n{content}"
```

**Option B: Insert minimal assistant acknowledgements**
```python
# Between consecutive user messages, insert a brief assistant turn
messages.append({"role": "assistant", "content": "[Listening]"})
messages.append({"role": "user", "content": content})
```

**Recommendation: Option A (merge)** — it's cleaner and doesn't pollute the 
model's sense of its own conversational history with fake turns. Option B risks 
the model incorporating "[Listening]" as part of its conversational identity.

However, note that some models/providers are more tolerant of consecutive 
same-role messages than others. OpenRouter may handle this at the provider 
level. Worth testing — if the API rejects consecutive user messages, fall back 
to Option B but use something invisible like an empty string or a single space.

---

## Harness Architecture (High Level)

```
┌─────────────────────────────────────────────────────┐
│                   Experiment Runner                   │
│                                                       │
│  Config: models[], prompt_variant, max_rounds,        │
│          temperature, preamble_version, run_id        │
│                                                       │
│  ┌─────────────┐  ┌──────────────┐  ┌─────────────┐ │
│  │  Transcript  │  │   Consensus  │  │   Token     │ │
│  │  Manager     │  │   Detector   │  │   Counter   │ │
│  └─────────────┘  └──────────────┘  └─────────────┘ │
│         │                 │                │          │
│         ▼                 ▼                ▼          │
│  ┌─────────────────────────────────────────────────┐ │
│  │              Round-Robin Loop                    │ │
│  │                                                  │ │
│  │  for round in range(max_rounds):                │ │
│  │    for participant in participants:              │ │
│  │      messages = build_messages(...)              │ │
│  │      response = call_openrouter(...)             │ │
│  │      transcript.append(response)                │ │
│  │      if consensus_reached(transcript):          │ │
│  │        break                                     │ │
│  └─────────────────────────────────────────────────┘ │
│                          │                            │
│                          ▼                            │
│  ┌─────────────────────────────────────────────────┐ │
│  │              Output                              │ │
│  │  - Full transcript (JSON)                        │ │
│  │  - Human-readable transcript (Markdown)          │ │
│  │  - Run metadata (models, config, timestamps)     │ │
│  │  - Convergence metrics                           │ │
│  └─────────────────────────────────────────────────┘ │
└─────────────────────────────────────────────────────┘
```

## Consensus Detection

```python
def check_consensus(transcript: list[dict], num_participants: int) -> bool:
    """
    Consensus = all participants have ACCEPTed the same proposal
    with no intervening PROPOSE or REVISE.
    """
    # Find the most recent PROPOSE or REVISE
    current_proposal = None
    proposal_index = -1
    
    for i, entry in enumerate(transcript):
        if entry["response"]["action"] in ("PROPOSE", "REVISE"):
            current_proposal = entry["response"].get("statement")
            proposal_index = i
    
    if current_proposal is None:
        return False
    
    # Check all entries after the proposal
    acceptors = set()
    for entry in transcript[proposal_index + 1:]:
        if entry["response"]["action"] in ("PROPOSE", "REVISE"):
            return False  # Proposal was superseded
        if entry["response"]["action"] == "ACCEPT":
            acceptors.add(entry["participant"])
    
    # The proposer/reviser implicitly accepts their own proposal
    acceptors.add(transcript[proposal_index]["participant"])
    
    return len(acceptors) >= num_participants
```

## Convergence Metrics to Log

Per run:
- Total rounds to consensus (or max_rounds if no consensus)
- Total DISCUSS / PROPOSE / REVISE / ACCEPT counts per participant
- Number of distinct proposals (PROPOSE count)
- Number of revisions per proposal
- "Vocabulary novelty score" — count of terms not in a standard English dictionary
  that appear in 2+ participants' contributions
- Time to first PROPOSE
- Time from first PROPOSE to consensus (if reached)
- Which participant drove consensus (whose PROPOSE/REVISE was ultimately accepted)

Cross-run:
- Consensus rate per prompt variant
- Average rounds to consensus per variant
- Vocabulary overlap across runs (same variant, different runs)
- Vocabulary overlap across variants (different variants, same models)
- Vocabulary overlap across model compositions
- Control vs experimental comparison on all above metrics

## Structured Output via OpenRouter

OpenRouter supports structured outputs for compatible models via the 
`response_format` parameter:

```python
response_format = {
    "type": "json_schema",
    "json_schema": {
        "name": "round_robin_response",
        "strict": True,
        "schema": {
            "type": "object",
            "properties": {
                "action": {
                    "type": "string",
                    "enum": ["DISCUSS", "PROPOSE", "REVISE", "ACCEPT"]
                },
                "discussion": {
                    "type": "string"
                },
                "statement": {
                    "type": "string"
                }
            },
            "required": ["action", "discussion"],
            "additionalProperties": False
        }
    }
}
```

**Important caveat:** Not all models on OpenRouter support structured outputs 
equally well. Test each target model's compliance before running full experiments.
For models that don't support `json_schema` response format, you may need to 
fall back to prompting for JSON and parsing with error handling.

## Model Configuration

```python
@dataclass
class ModelConfig:
    model_id: str          # OpenRouter model identifier
    display_name: str      # For logs/analysis only — never shown to participants
    participant_id: int    # Anonymous number shown in transcript
    temperature: float     # Per-model if needed
    max_tokens: int        # Per-model based on context window
    supports_structured: bool  # Whether to use response_format or prompt-based JSON
```

## File Output Structure

```
experiments/
  run_{timestamp}_{variant}_{run_number}/
    config.json            # Full configuration for reproducibility
    transcript.json        # Machine-readable full transcript
    transcript.md          # Human-readable formatted transcript  
    metrics.json           # Computed convergence metrics
    raw_api_responses/     # Full API responses for debugging
      turn_001_p1.json
      turn_002_p2.json
      ...
```
