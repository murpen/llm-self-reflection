"""Model configuration, API calls, and message construction."""

import json
import logging
import re
import time
from dataclasses import dataclass
from pathlib import Path

import yaml
from openai import OpenAI

logger = logging.getLogger(__name__)


# --- Dataclasses ---

@dataclass
class ModelConfig:
    """Configuration for a single model participant."""
    model_id: str
    display_name: str
    temperature: float = 0.7
    max_tokens: int = 2048
    supports_structured: bool = True
    reasoning: bool = False
    reasoning_effort: str | None = None  # "low", "medium", or "high"
    reasoning_max_tokens: int | None = 8000  # Token budget for thinking


@dataclass
class ExperimentConfig:
    """Configuration for an experiment run."""
    models: list[ModelConfig]
    prompt_variant: str
    preamble_version: str = "A"
    max_rounds: int = 20
    run_count: int = 1
    output_dir: str = "runs"


def load_config(yaml_path: str) -> ExperimentConfig:
    """Load experiment configuration from a YAML file."""
    path = Path(yaml_path)
    if not path.exists():
        raise FileNotFoundError(f"Config file not found: {yaml_path}")

    with open(path) as f:
        raw = yaml.safe_load(f)

    models = []
    for m in raw["models"]:
        models.append(ModelConfig(
            model_id=m["model_id"],
            display_name=m["display_name"],
            temperature=m.get("temperature", 0.7),
            max_tokens=m.get("max_tokens", 2048),
            supports_structured=m.get("supports_structured", True),
            reasoning=m.get("reasoning", False),
            reasoning_effort=m.get("reasoning_effort"),
            reasoning_max_tokens=m.get("reasoning_max_tokens", 8000),
        ))

    return ExperimentConfig(
        models=models,
        prompt_variant=raw["prompt_variant"],
        preamble_version=raw.get("preamble_version", "A"),
        max_rounds=raw.get("max_rounds", 20),
        run_count=raw.get("run_count", 1),
        output_dir=raw.get("output_dir", "runs"),
    )


# --- Structured Output Schema ---

# All three fields required for strict mode; statement can be empty string.
RESPONSE_SCHEMA = {
    "type": "json_schema",
    "json_schema": {
        "name": "round_robin_response",
        "strict": True,
        "schema": {
            "type": "object",
            "properties": {
                "action": {
                    "type": "string",
                    "enum": ["DISCUSS", "PROPOSE", "REVISE", "ACCEPT"],
                },
                "discussion": {
                    "type": "string",
                },
                "statement": {
                    "type": "string",
                },
            },
            "required": ["action", "discussion", "statement"],
            "additionalProperties": False,
        },
    },
}

# Instruction appended to the final user message for models without
# structured output support.
JSON_INSTRUCTION = """

You MUST respond with valid JSON matching this schema:
{
  "action": "DISCUSS" | "PROPOSE" | "REVISE" | "ACCEPT",
  "discussion": "<your reasoning/commentary>",
  "statement": "<consensus statement text, or empty string if not applicable>"
}

Respond with ONLY the JSON object. No other text before or after."""


# --- API Calls ---

def call_openrouter(
    client: OpenAI,
    model_config: ModelConfig,
    messages: list[dict],
    turn_number: int,
    participant_id: int,
    raw_responses_dir: Path,
) -> tuple[dict, dict]:
    """Call OpenRouter and return (parsed_response, metadata).

    Retries up to 3 times with exponential backoff on failure.
    Saves the full raw API response to disk.
    """
    max_retries = 3
    backoff_delays = [2, 8, 32]

    for attempt in range(max_retries):
        try:
            kwargs = {
                "model": model_config.model_id,
                "messages": messages,
                "temperature": model_config.temperature,
                "max_tokens": model_config.max_tokens,
            }
            # Reasoning and response_format are incompatible — when reasoning
            # is enabled, we fall back to prompt-based JSON extraction.
            if model_config.supports_structured and not model_config.reasoning:
                kwargs["response_format"] = RESPONSE_SCHEMA

            # Enable reasoning/thinking for supported models
            if model_config.reasoning:
                reasoning_config = {
                    "max_tokens": model_config.reasoning_max_tokens,
                }
                if model_config.reasoning_effort:
                    reasoning_config["effort"] = model_config.reasoning_effort
                kwargs["extra_body"] = {"reasoning": reasoning_config}

            response = client.chat.completions.create(**kwargs)

            # Save raw response — use attempt suffix so retries don't
            # overwrite earlier failures
            suffix = "" if attempt == 0 else f"_attempt{attempt + 1}"
            raw_path = (
                raw_responses_dir
                / f"turn_{turn_number:03d}_p{participant_id}{suffix}.json"
            )
            raw_data = response.model_dump()
            with open(raw_path, "w") as f:
                json.dump(raw_data, f, indent=2, default=str)

            # Extract content
            message = response.choices[0].message
            content = message.content or ""

            # Extract reasoning trace if present
            reasoning = getattr(message, "reasoning", None) or None

            # Parse JSON — use permissive parsing when reasoning is enabled
            # (since response_format is disabled) or when structured output
            # isn't supported
            use_strict = (
                model_config.supports_structured and not model_config.reasoning
            )
            if use_strict:
                parsed = json.loads(content)
            else:
                parsed = parse_response_permissive(content)

            # Build metadata
            usage = response.usage
            metadata = {
                "model_id": model_config.model_id,
                "prompt_tokens": usage.prompt_tokens if usage else 0,
                "completion_tokens": usage.completion_tokens if usage else 0,
                "total_tokens": usage.total_tokens if usage else 0,
                "finish_reason": response.choices[0].finish_reason,
            }
            if reasoning:
                metadata["reasoning"] = reasoning

            logger.info(
                "Turn %d, Participant %d (%s): action=%s, tokens=%d/%d%s",
                turn_number, participant_id, model_config.display_name,
                parsed.get("action", "?"),
                metadata["prompt_tokens"], metadata["completion_tokens"],
                " [reasoning]" if reasoning else "",
            )

            return parsed, metadata

        except Exception as e:
            if attempt < max_retries - 1:
                delay = backoff_delays[attempt]
                logger.warning(
                    "Attempt %d/%d failed for %s: %s. Retrying in %ds...",
                    attempt + 1, max_retries, model_config.display_name,
                    str(e), delay,
                )
                time.sleep(delay)
            else:
                logger.error(
                    "All %d attempts failed for %s: %s",
                    max_retries, model_config.display_name, str(e),
                )
                raise


def _repair_truncated_json(fragment: str) -> dict | None:
    """Attempt to repair truncated JSON by closing open strings and braces.

    Handles the common case where a model's response is cut off mid-value,
    e.g. {"action": "ACCEPT", "discussion": "I agree because...
    """
    # Try progressively aggressive repairs
    repairs = [
        '',          # Maybe it's just missing whitespace
        '"',         # Close an open string value
        '"}',        # Close string + object
        '""}',       # Close string + empty last field + object
        '"}',        # Close string + object (statement may be missing)
    ]

    # Also try adding the missing fields if we can detect the structure
    for repair in repairs:
        try:
            candidate = fragment + repair
            result = json.loads(candidate)
            if isinstance(result, dict) and "action" in result:
                # Ensure required fields exist
                result.setdefault("discussion", "")
                result.setdefault("statement", "")
                return result
        except json.JSONDecodeError:
            continue

    # More aggressive: try to extract whatever fields we can via regex
    action_match = re.search(
        r'"action"\s*:\s*"(DISCUSS|PROPOSE|REVISE|ACCEPT)"', fragment
    )
    discussion_match = re.search(
        r'"discussion"\s*:\s*"((?:[^"\\]|\\.)*)', fragment, re.DOTALL
    )
    statement_match = re.search(
        r'"statement"\s*:\s*"((?:[^"\\]|\\.)*)', fragment, re.DOTALL
    )

    if action_match:
        return {
            "action": action_match.group(1),
            "discussion": discussion_match.group(1) if discussion_match else "",
            "statement": statement_match.group(1) if statement_match else "",
        }

    return None


def parse_response_permissive(content: str) -> dict:
    """Extract JSON from model output that may have markdown fences or preamble.

    Tries multiple strategies:
    1. Direct JSON parse
    2. Extract from markdown code fences
    3. Find first { to last } and parse
    4. Repair truncated JSON (missing closing quotes/braces)
    5. Free-form text starting with an action keyword
    """
    content = content.strip()

    # Strategy 1: direct parse
    try:
        return json.loads(content)
    except json.JSONDecodeError:
        pass

    # Strategy 2: markdown code fences
    fence_match = re.search(r"```(?:json)?\s*\n?(.*?)\n?```", content, re.DOTALL)
    if fence_match:
        try:
            return json.loads(fence_match.group(1).strip())
        except json.JSONDecodeError:
            pass

    # Strategy 3: brace extraction
    first_brace = content.find("{")
    last_brace = content.rfind("}")
    if first_brace != -1 and last_brace > first_brace:
        try:
            return json.loads(content[first_brace:last_brace + 1])
        except json.JSONDecodeError:
            pass

    # Strategy 4: repair truncated JSON — models sometimes get cut off
    # mid-response, producing valid JSON that's missing closing elements
    if first_brace != -1:
        fragment = content[first_brace:]
        repaired = _repair_truncated_json(fragment)
        if repaired is not None:
            return repaired

    # Strategy 5: free-form text starting with an action keyword
    # (models with reasoning enabled sometimes output plain text)
    action_match = re.match(
        r"^(DISCUSS|PROPOSE|REVISE|ACCEPT)\s*:\s*(.*)",
        content,
        re.DOTALL,
    )
    if action_match:
        action = action_match.group(1)
        text = action_match.group(2).strip()

        # Try to split out a statement for PROPOSE/REVISE
        statement = ""
        if action in ("PROPOSE", "REVISE"):
            # Look for a statement block — common patterns
            stmt_match = re.search(
                r"(?:Statement|Proposal|Consensus statement)\s*:\s*(.*)",
                text,
                re.DOTALL | re.IGNORECASE,
            )
            if stmt_match:
                statement = stmt_match.group(1).strip()

        return {
            "action": action,
            "discussion": text,
            "statement": statement,
        }

    raise ValueError(f"Could not parse JSON from model response: {content[:200]}...")


# --- Message Construction ---

def format_turn(entry: dict) -> str:
    """Format a transcript entry for inclusion in messages.

    Uses anonymous participant numbers only — no model names.
    """
    p = entry["participant"]
    r = entry["response"]
    action = r["action"]

    parts = [f"[Participant {p}]"]
    parts.append(f"Action: {action}")

    if r.get("discussion"):
        parts.append(f"Discussion: {r['discussion']}")

    if r.get("statement"):
        parts.append(f"Statement: {r['statement']}")

    return "\n".join(parts)


def build_messages(
    participant_id: int,
    transcript: list[dict],
    system_prompt: str,
    supports_structured: bool,
) -> list[dict]:
    """Build the messages array for a participant's turn.

    Own previous turns use 'assistant' role; other participants' turns
    use 'user' role. Consecutive same-role messages are merged with a
    separator to satisfy API alternation requirements.
    """
    messages = [{"role": "system", "content": system_prompt}]

    for entry in transcript:
        content = format_turn(entry)

        if entry["participant"] == participant_id:
            role = "assistant"
        else:
            role = "user"

        # Merge consecutive same-role messages
        if messages and messages[-1]["role"] == role:
            messages[-1]["content"] += f"\n\n---\n\n{content}"
        else:
            messages.append({"role": role, "content": content})

    # Final user message to prompt the model's turn
    turn_prompt = f"It is now your turn (Participant {participant_id}). Respond following the protocol."

    # For models without structured output support, append JSON instructions
    if not supports_structured:
        turn_prompt += JSON_INSTRUCTION

    if messages and messages[-1]["role"] == "user":
        messages[-1]["content"] += f"\n\n---\n\n{turn_prompt}"
    else:
        messages.append({"role": "user", "content": turn_prompt})

    return messages
