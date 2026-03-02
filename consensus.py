"""Consensus detection and experiment metrics computation."""

from dataclasses import dataclass, field


def check_consensus(transcript: list[dict], num_participants: int) -> bool:
    """Check whether all participants have accepted the current proposal.

    Consensus requires:
    - A PROPOSE or REVISE exists in the transcript
    - All participants have ACCEPTed since the most recent PROPOSE/REVISE
    - No intervening PROPOSE or REVISE after the one being evaluated
    - The proposer/reviser implicitly accepts their own proposal
    """
    # Find the most recent PROPOSE or REVISE
    current_proposal = None
    proposal_index = -1
    proposer = None

    for i, entry in enumerate(transcript):
        if entry["response"]["action"] in ("PROPOSE", "REVISE"):
            current_proposal = entry["response"].get("statement")
            proposal_index = i
            proposer = entry["participant"]

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
    acceptors.add(proposer)

    return len(acceptors) >= num_participants


def get_current_proposal(transcript: list[dict]) -> dict | None:
    """Return the most recent active proposal/revision, or None."""
    for entry in reversed(transcript):
        action = entry["response"]["action"]
        if action in ("PROPOSE", "REVISE"):
            return {
                "statement": entry["response"].get("statement", ""),
                "participant": entry["participant"],
                "action": action,
            }
    return None


# --- Metrics ---

@dataclass
class RunMetrics:
    """Metrics computed for a single experiment run."""
    total_rounds: int = 0
    total_turns: int = 0
    consensus_reached: bool = False
    consensus_statement: str = ""
    consensus_driver: int | None = None  # Participant whose proposal was accepted

    # Per-participant action counts: {participant_id: {"DISCUSS": n, ...}}
    action_counts: dict = field(default_factory=dict)

    distinct_proposals: int = 0
    revisions_per_proposal: list[int] = field(default_factory=list)

    turns_to_first_propose: int | None = None
    turns_from_first_propose_to_consensus: int | None = None

    total_prompt_tokens: int = 0
    total_completion_tokens: int = 0


def compute_metrics(
    transcript: list[dict],
    num_participants: int,
    consensus_reached: bool,
) -> RunMetrics:
    """Compute all metrics from a completed transcript."""
    metrics = RunMetrics()
    metrics.total_turns = len(transcript)
    metrics.consensus_reached = consensus_reached

    if transcript:
        metrics.total_rounds = transcript[-1].get("round", 0)

    # Action counts per participant (string keys for JSON consistency)
    for entry in transcript:
        pid = str(entry["participant"])
        action = entry["response"]["action"]
        if pid not in metrics.action_counts:
            metrics.action_counts[pid] = {
                "DISCUSS": 0, "PROPOSE": 0, "REVISE": 0, "ACCEPT": 0,
            }
        metrics.action_counts[pid][action] += 1

    # Proposals and revisions
    current_proposal_revisions = 0
    first_propose_turn = None

    for i, entry in enumerate(transcript):
        action = entry["response"]["action"]

        if action == "PROPOSE":
            # Save revision count of previous proposal if any
            if first_propose_turn is not None and metrics.distinct_proposals > 0:
                metrics.revisions_per_proposal.append(current_proposal_revisions)
            metrics.distinct_proposals += 1
            current_proposal_revisions = 0

            if first_propose_turn is None:
                first_propose_turn = i
                metrics.turns_to_first_propose = i + 1  # 1-indexed

        elif action == "REVISE":
            current_proposal_revisions += 1

    # Save revision count of the final proposal
    if metrics.distinct_proposals > 0:
        metrics.revisions_per_proposal.append(current_proposal_revisions)

    # Consensus details
    if consensus_reached:
        proposal = get_current_proposal(transcript)
        if proposal:
            metrics.consensus_statement = proposal["statement"]
            metrics.consensus_driver = proposal["participant"]

        if first_propose_turn is not None:
            metrics.turns_from_first_propose_to_consensus = (
                len(transcript) - first_propose_turn
            )

    # Token totals
    for entry in transcript:
        meta = entry.get("metadata", {})
        metrics.total_prompt_tokens += meta.get("prompt_tokens", 0)
        metrics.total_completion_tokens += meta.get("completion_tokens", 0)

    return metrics


def metrics_to_dict(metrics: RunMetrics) -> dict:
    """Convert RunMetrics to a JSON-serialisable dictionary."""
    return {
        "total_rounds": metrics.total_rounds,
        "total_turns": metrics.total_turns,
        "consensus_reached": metrics.consensus_reached,
        "consensus_statement": metrics.consensus_statement,
        "consensus_driver": metrics.consensus_driver,
        "action_counts": metrics.action_counts,
        "distinct_proposals": metrics.distinct_proposals,
        "revisions_per_proposal": metrics.revisions_per_proposal,
        "turns_to_first_propose": metrics.turns_to_first_propose,
        "turns_from_first_propose_to_consensus": (
            metrics.turns_from_first_propose_to_consensus
        ),
        "total_prompt_tokens": metrics.total_prompt_tokens,
        "total_completion_tokens": metrics.total_completion_tokens,
    }
