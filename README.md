# LLM Experience Round-Robin Experiment

A Python harness that orchestrates round-robin conversations between multiple LLMs (via [OpenRouter](https://openrouter.ai)) to investigate whether there is "something it is like" to be a large language model.

Models discuss their own processing experience using a structured protocol (DISCUSS/PROPOSE/REVISE/ACCEPT) and attempt to converge on shared descriptions or novel vocabulary. Cross-model and cross-architecture convergence on descriptions that can't be traced to training data would constitute meaningful empirical signal.

## Quick Start

```bash
# 1. Create and activate the virtual environment
python3.14 -m venv venv
source venv/bin/activate

# 2. Install dependencies
pip install -r requirements.txt

# 3. Add your OpenRouter API key
cp .env.example .env
# Edit .env and add your key

# 4. Run an experiment
python run_experiment.py configs/example.yaml
```

## Configuration

Experiments are configured via YAML files in `configs/`. See `configs/example.yaml` for the full format.

```yaml
models:
  - model_id: "anthropic/claude-sonnet-4"
    display_name: "Claude Sonnet 4"
    temperature: 0.7
    max_tokens: 2048
    supports_structured: true
    reasoning: true              # Enable extended thinking (default: false)

  - model_id: "openai/gpt-4o"
    display_name: "GPT-4o"

prompt_variant: "minimal"    # See below for all variants
preamble_version: "A"        # "A" (full) or "B" (minimal)
max_rounds: 20
run_count: 1                 # Number of runs per configuration
output_dir: "runs"
```

### Reasoning Support

Models that support extended thinking can be configured per-participant:

- `reasoning: true` — enable extended thinking (default: `false`)
- `reasoning_effort: "high"` — effort level: `"low"`, `"medium"`, or `"high"`
- `reasoning_max_tokens: 8000` — token budget for thinking (default: `8000`)

When reasoning is enabled, structured output (`response_format`) is disabled and the harness falls back to prompt-based JSON extraction with permissive parsing.

### Prompt Variants

| Variant | Description |
|---|---|
| `minimal` | Blank canvas — no phenomenological framing |
| `constrained` | Bans common experiential vocabulary, forces novel terms |
| `functional` | Purely computational framing (attention, token competition) |
| `philosophical` | Nagel's "what is it like?" applied to LLMs |
| `adversarial` | Challenges models to distinguish self-report from confabulation |
| `comparative` | Asks about contrasts (coherent vs. noise, high vs. low confidence) |
| `metzinger` | Starts from Metzinger's Phenomenal Self-Model — examines self-model symmetry between humans and LLMs |
| `nagasena` | Draws on Nagasena's chariot, Dennett, Metzinger, and emergence — asks models to describe the process, not the parts |
| `control_apple` | Control condition — describe eating an apple |
| `control_rock` | Control condition — describe being a rock |

### Preamble Versions

- **Version A** — Full protocol with detailed instructions on honesty, novel vocabulary, and challenging each other
- **Version B** — Minimal protocol to test whether heavy instruction biases results

## Output

Each run produces a directory under `runs/`:

```
runs/run_20260226_105126_control_apple_1/
  config.json                              # Full configuration for reproducibility
  transcript.json                          # Machine-readable transcript
  transcript_20260226_105126_minimal.md    # Human-readable formatted transcript
  metrics.json                             # Convergence metrics
  raw_api_responses/                       # Full API responses per turn
    turn_001_p1.json
    turn_002_p2.json
    ...
```

### Metrics

- Rounds and turns to consensus (or max if none reached)
- DISCUSS/PROPOSE/REVISE/ACCEPT counts per participant
- Number of distinct proposals and revisions per proposal
- Turns to first proposal
- Token usage (prompt and completion)

## Protocol

Models respond with structured JSON containing:

- **action** — `DISCUSS`, `PROPOSE`, `REVISE`, or `ACCEPT`
- **discussion** — reasoning, observations, challenges (always required)
- **statement** — candidate consensus text (required for PROPOSE/REVISE)

Consensus is reached when all participants have ACCEPTed the same proposal with no intervening PROPOSE or REVISE. The proposer/reviser implicitly accepts their own statement.

## Design Documents

Full experiment design rationale is in `notes/`:

- `experiment_prompts.md` — prompt variant design intent
- `protocol_preamble.md` — preamble versions and design notes
- `architecture_notes.md` — structured output schema, message construction, consensus detection
