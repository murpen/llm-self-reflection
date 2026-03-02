#!/usr/bin/env python3
"""CLI entry point for running LLM round-robin experiments."""

import argparse
import logging
import sys

from experiment import run_experiment
from models import load_config
from prompts import PROMPTS


def main():
    parser = argparse.ArgumentParser(
        description="Run an LLM round-robin experiment via OpenRouter.",
    )
    parser.add_argument(
        "config",
        help="Path to YAML configuration file",
    )
    parser.add_argument(
        "-p", "--prompt",
        choices=list(PROMPTS.keys()),
        help="Override the prompt variant from the config file",
    )
    parser.add_argument(
        "-v", "--verbose",
        action="store_true",
        help="Enable debug logging",
    )
    args = parser.parse_args()

    # Set up logging
    log_level = logging.DEBUG if args.verbose else logging.INFO
    logging.basicConfig(
        level=log_level,
        format="%(asctime)s %(levelname)-8s %(name)s — %(message)s",
        datefmt="%H:%M:%S",
    )

    try:
        config = load_config(args.config)
    except (FileNotFoundError, KeyError, ValueError) as e:
        logging.error("Failed to load config: %s", e)
        sys.exit(1)

    if args.prompt:
        config.prompt_variant = args.prompt

    output_dirs = run_experiment(config)

    print()
    print("Experiment complete. Output directories:")
    for d in output_dirs:
        print(f"  {d}")


if __name__ == "__main__":
    main()
