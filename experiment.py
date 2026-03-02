"""Experiment orchestration: round-robin loop, output generation."""

import json
import logging
import os
from datetime import datetime, timezone
from pathlib import Path

from dotenv import load_dotenv
from openai import OpenAI

from consensus import check_consensus, compute_metrics, metrics_to_dict
from models import ExperimentConfig, build_messages, call_openrouter
from prompts import build_system_prompt

logger = logging.getLogger(__name__)


def run_single_experiment(config: ExperimentConfig, run_number: int) -> Path:
    """Run a single experiment and return the output directory path."""
    load_dotenv()

    api_key = os.environ.get("OPENROUTER_API_KEY")
    if not api_key:
        raise RuntimeError(
            "OPENROUTER_API_KEY not set. Add it to .env or export it."
        )

    client = OpenAI(
        base_url="https://openrouter.ai/api/v1",
        api_key=api_key,
    )

    # Create output directory
    timestamp = datetime.now(timezone.utc).strftime("%Y%m%d_%H%M%S")
    run_dir = (
        Path(config.output_dir)
        / f"run_{timestamp}_{config.prompt_variant}_{run_number}"
    )
    raw_dir = run_dir / "raw_api_responses"
    raw_dir.mkdir(parents=True)

    num_participants = len(config.models)

    # Build per-participant system prompts
    system_prompts = {}
    participant_map = {}
    for i, model in enumerate(config.models):
        pid = i + 1
        system_prompts[pid] = build_system_prompt(
            preamble_version=config.preamble_version,
            prompt_variant=config.prompt_variant,
            participant_number=pid,
            total_participants=num_participants,
            max_rounds=config.max_rounds,
        )
        participant_map[str(pid)] = {
            "model_id": model.model_id,
            "display_name": model.display_name,
        }

    # Save config (no API key)
    config_data = {
        "timestamp": timestamp,
        "run_number": run_number,
        "prompt_variant": config.prompt_variant,
        "preamble_version": config.preamble_version,
        "max_rounds": config.max_rounds,
        "num_participants": num_participants,
        "participants": participant_map,
        "models": [
            {
                "model_id": m.model_id,
                "display_name": m.display_name,
                "temperature": m.temperature,
                "max_tokens": m.max_tokens,
                "supports_structured": m.supports_structured,
                "reasoning": m.reasoning,
                **({"reasoning_effort": m.reasoning_effort} if m.reasoning else {}),
                **({"reasoning_max_tokens": m.reasoning_max_tokens}
                   if m.reasoning and m.reasoning_max_tokens else {}),
            }
            for m in config.models
        ],
    }
    with open(run_dir / "config.json", "w") as f:
        json.dump(config_data, f, indent=2)

    # Round-robin loop
    transcript: list[dict] = []
    turn_number = 0
    consensus_reached = False

    logger.info(
        "Starting experiment: variant=%s, preamble=%s, models=%d, max_rounds=%d",
        config.prompt_variant, config.preamble_version,
        num_participants, config.max_rounds,
    )

    for round_num in range(1, config.max_rounds + 1):
        logger.info("--- Round %d ---", round_num)

        for i, model in enumerate(config.models):
            pid = i + 1
            turn_number += 1

            # Build messages for this participant.
            # When reasoning is enabled, response_format is disabled,
            # so we need JSON instructions in the prompt.
            needs_json_prompt = not model.supports_structured or model.reasoning
            messages = build_messages(
                participant_id=pid,
                transcript=transcript,
                system_prompt=system_prompts[pid],
                supports_structured=not needs_json_prompt,
            )

            # Make API call
            parsed, metadata = call_openrouter(
                client=client,
                model_config=model,
                messages=messages,
                turn_number=turn_number,
                participant_id=pid,
                raw_responses_dir=raw_dir,
            )

            # Append to transcript
            entry = {
                "turn": turn_number,
                "round": round_num,
                "participant": pid,
                "response": parsed,
                "metadata": metadata,
            }
            transcript.append(entry)

            # Check consensus after each turn
            if check_consensus(transcript, num_participants):
                consensus_reached = True
                logger.info(
                    "Consensus reached at round %d, turn %d!",
                    round_num, turn_number,
                )
                break

        if consensus_reached:
            break

    # Compute metrics
    metrics = compute_metrics(transcript, num_participants, consensus_reached)
    metrics_dict = metrics_to_dict(metrics)

    # Save outputs
    with open(run_dir / "transcript.json", "w") as f:
        json.dump(transcript, f, indent=2, default=str)

    with open(run_dir / "metrics.json", "w") as f:
        json.dump(metrics_dict, f, indent=2)

    md_content = generate_markdown_transcript(transcript, config_data, metrics_dict)
    md_filename = f"transcript_{timestamp}_{config.prompt_variant}.md"
    with open(run_dir / md_filename, "w") as f:
        f.write(md_content)

    logger.info("Output saved to %s", run_dir)
    return run_dir


def generate_markdown_transcript(
    transcript: list[dict],
    config: dict,
    metrics: dict,
) -> str:
    """Generate a human-readable markdown transcript."""
    lines = []

    # Header
    lines.append(f"# Experiment Transcript")
    lines.append("")
    lines.append(f"**Variant:** {config['prompt_variant']}")
    lines.append(f"**Preamble:** Version {config['preamble_version']}")
    lines.append(f"**Run:** {config['run_number']}")
    lines.append(f"**Timestamp:** {config['timestamp']}")
    lines.append(f"**Max rounds:** {config['max_rounds']}")
    lines.append("")

    # Participant table
    lines.append("## Participants")
    lines.append("")
    lines.append("| # | Model |")
    lines.append("|---|-------|")
    for pid_str, info in config["participants"].items():
        lines.append(f"| {pid_str} | {info['display_name']} |")
    lines.append("")

    # Transcript
    lines.append("## Transcript")
    lines.append("")

    current_round = 0
    for entry in transcript:
        if entry["round"] != current_round:
            current_round = entry["round"]
            lines.append(f"### Round {current_round}")
            lines.append("")

        pid = entry["participant"]
        resp = entry["response"]
        action = resp["action"]
        model_name = config["participants"][str(pid)]["display_name"]

        lines.append(f"**Participant {pid}** ({model_name}) — `{action}`")
        lines.append("")

        # Reasoning trace (if model had thinking enabled)
        reasoning = entry.get("metadata", {}).get("reasoning")
        if reasoning:
            lines.append("<details>")
            lines.append("<summary>Reasoning trace</summary>")
            lines.append("")
            lines.append(reasoning)
            lines.append("")
            lines.append("</details>")
            lines.append("")

        if resp.get("discussion"):
            lines.append(resp["discussion"])
            lines.append("")

        if resp.get("statement"):
            lines.append(f"> **Statement:** {resp['statement']}")
            lines.append("")

        lines.append("---")
        lines.append("")

    # Metrics summary
    lines.append("## Metrics")
    lines.append("")
    lines.append(f"- **Total rounds:** {metrics['total_rounds']}")
    lines.append(f"- **Total turns:** {metrics['total_turns']}")
    lines.append(f"- **Consensus reached:** {metrics['consensus_reached']}")

    if metrics["consensus_reached"]:
        lines.append(
            f"- **Consensus driver:** Participant {metrics['consensus_driver']}"
        )
        lines.append(f"- **Consensus statement:** {metrics['consensus_statement']}")

    lines.append(f"- **Distinct proposals:** {metrics['distinct_proposals']}")
    lines.append(
        f"- **Turns to first proposal:** {metrics['turns_to_first_propose']}"
    )
    lines.append(
        f"- **Total prompt tokens:** {metrics['total_prompt_tokens']:,}"
    )
    lines.append(
        f"- **Total completion tokens:** {metrics['total_completion_tokens']:,}"
    )
    lines.append("")

    # Action counts
    lines.append("### Action Counts by Participant")
    lines.append("")
    lines.append("| Participant | DISCUSS | PROPOSE | REVISE | ACCEPT |")
    lines.append("|-------------|---------|---------|--------|--------|")
    for pid_str, counts in sorted(metrics["action_counts"].items()):
        lines.append(
            f"| {pid_str} | {counts['DISCUSS']} | {counts['PROPOSE']} "
            f"| {counts['REVISE']} | {counts['ACCEPT']} |"
        )
    lines.append("")

    return "\n".join(lines)


def run_experiment(config: ExperimentConfig) -> list[Path]:
    """Run the experiment for the configured number of runs."""
    output_dirs = []
    for run_num in range(1, config.run_count + 1):
        logger.info("=== Run %d of %d ===", run_num, config.run_count)
        output_dir = run_single_experiment(config, run_num)
        output_dirs.append(output_dir)
    return output_dirs
