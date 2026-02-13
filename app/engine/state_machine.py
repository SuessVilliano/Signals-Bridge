"""
Signal State Machine - manages lifecycle transitions.

This module implements a deterministic finite state machine (FSM) for managing
the lifecycle of trading signals. It enforces valid state transitions, ensures
idempotency, prevents rollbacks, and provides detailed transition information.

Valid transitions:
- PENDING → ACTIVE (entry price reached)
- PENDING → INVALID (validation failed)
- PENDING → CLOSED (expired, cancelled)
- ACTIVE → TP1_HIT (tp1 reached)
- ACTIVE → SL_HIT (stop loss hit)
- ACTIVE → CLOSED (manual close)
- TP1_HIT → TP2_HIT (tp2 reached)
- TP1_HIT → SL_HIT (stop hit after tp1)
- TP1_HIT → CLOSED (manual close)
- TP2_HIT → TP3_HIT (tp3 reached)
- TP2_HIT → SL_HIT (stop hit after tp2)
- TP2_HIT → CLOSED (manual close)
- TP3_HIT → CLOSED (all TPs hit, fully closed)

The state machine is:
- Pure (no side effects or database access)
- Idempotent (same event twice = no change)
- Irreversible (no rollbacks after SL_HIT or terminal states)
"""

from enum import Enum
from typing import Tuple, Dict, Set, Optional
from dataclasses import dataclass

from app.models.canonical_signal import SignalStatus, EventType


@dataclass
class TransitionResult:
    """Result of a state transition attempt."""

    new_status: SignalStatus
    did_transition: bool  # True if state actually changed
    reason: str  # Human-readable explanation
    is_terminal: bool  # True if new status is terminal


class SignalStateMachine:
    """
    Manages signal lifecycle state transitions.

    This is a pure, stateless implementation. All methods are static and
    deterministic based solely on the input parameters.
    """

    # Define all valid transitions
    VALID_TRANSITIONS: Dict[SignalStatus, Set[SignalStatus]] = {
        SignalStatus.PENDING: {
            SignalStatus.ACTIVE,
            SignalStatus.INVALID,
            SignalStatus.CLOSED,
        },
        SignalStatus.ACTIVE: {
            SignalStatus.TP1_HIT,
            SignalStatus.SL_HIT,
            SignalStatus.CLOSED,
        },
        SignalStatus.TP1_HIT: {
            SignalStatus.TP2_HIT,
            SignalStatus.SL_HIT,
            SignalStatus.CLOSED,
        },
        SignalStatus.TP2_HIT: {
            SignalStatus.TP3_HIT,
            SignalStatus.SL_HIT,
            SignalStatus.CLOSED,
        },
        SignalStatus.TP3_HIT: {
            SignalStatus.CLOSED,
        },
        SignalStatus.SL_HIT: set(),  # Terminal: no transitions
        SignalStatus.CLOSED: set(),  # Terminal: no transitions
        SignalStatus.INVALID: set(),  # Terminal: no transitions
    }

    # Map EventType to resulting SignalStatus
    EVENT_TO_STATUS: Dict[EventType, SignalStatus] = {
        EventType.ENTRY_HIT: SignalStatus.ACTIVE,
        EventType.TP1_HIT: SignalStatus.TP1_HIT,
        EventType.TP2_HIT: SignalStatus.TP2_HIT,
        EventType.TP3_HIT: SignalStatus.TP3_HIT,
        EventType.SL_HIT: SignalStatus.SL_HIT,
        EventType.MANUAL_CLOSE: SignalStatus.CLOSED,
        EventType.VALIDATION_FAILED: SignalStatus.INVALID,
        EventType.EXPIRED: SignalStatus.CLOSED,
    }

    # Terminal states from which no further transitions are possible
    TERMINAL_STATES: Set[SignalStatus] = {
        SignalStatus.CLOSED,
        SignalStatus.INVALID,
        SignalStatus.SL_HIT,
    }

    # Status to close reason mapping
    CLOSE_REASONS: Dict[SignalStatus, str] = {
        SignalStatus.CLOSED: "Manual close or expired",
        SignalStatus.SL_HIT: "Stop-loss triggered",
        SignalStatus.INVALID: "Signal validation failed",
        SignalStatus.TP3_HIT: "All take-profits hit",
    }

    @staticmethod
    def can_transition(current: SignalStatus, target: SignalStatus) -> bool:
        """
        Check if a transition from current to target status is allowed.

        Args:
            current: Current signal status
            target: Target signal status

        Returns:
            True if transition is valid, False otherwise
        """
        if current not in SignalStateMachine.VALID_TRANSITIONS:
            return False

        valid_targets = SignalStateMachine.VALID_TRANSITIONS[current]
        return target in valid_targets

    @staticmethod
    def process_event(
        current_status: SignalStatus,
        event_type: EventType,
    ) -> TransitionResult:
        """
        Process an event and return the resulting state.

        This is the main entry point for state transitions. It's idempotent:
        calling it multiple times with the same event results in the same
        final state (no duplicate transitions).

        Args:
            current_status: Current status of the signal
            event_type: Event that occurred

        Returns:
            TransitionResult with new status, whether transition occurred, and reason
        """
        # Map event to target status
        target_status = SignalStateMachine.EVENT_TO_STATUS.get(event_type)

        if target_status is None:
            return TransitionResult(
                new_status=current_status,
                did_transition=False,
                reason=f"Unknown event type: {event_type}",
                is_terminal=SignalStateMachine.is_terminal(current_status),
            )

        # Check if already in target status (idempotent)
        if current_status == target_status:
            return TransitionResult(
                new_status=current_status,
                did_transition=False,
                reason=f"Already in {target_status} state",
                is_terminal=SignalStateMachine.is_terminal(current_status),
            )

        # Check if transition is valid
        if not SignalStateMachine.can_transition(current_status, target_status):
            return TransitionResult(
                new_status=current_status,
                did_transition=False,
                reason=(
                    f"Invalid transition: {current_status} → {target_status}"
                ),
                is_terminal=SignalStateMachine.is_terminal(current_status),
            )

        # Check if current state is terminal (no further transitions allowed)
        if SignalStateMachine.is_terminal(current_status):
            return TransitionResult(
                new_status=current_status,
                did_transition=False,
                reason=f"Cannot transition from terminal state {current_status}",
                is_terminal=True,
            )

        # Transition is valid
        return TransitionResult(
            new_status=target_status,
            did_transition=True,
            reason=f"Transitioned: {current_status} → {target_status}",
            is_terminal=SignalStateMachine.is_terminal(target_status),
        )

    @staticmethod
    def is_terminal(status: SignalStatus) -> bool:
        """
        Check if a status is terminal (no further transitions possible).

        Terminal states represent signal closure: the signal is no longer open
        and no further state transitions are allowed.

        Args:
            status: Signal status to check

        Returns:
            True if status is terminal, False otherwise
        """
        return status in SignalStateMachine.TERMINAL_STATES

    @staticmethod
    def get_close_reason(status: SignalStatus) -> Optional[str]:
        """
        Get human-readable close reason for a terminal status.

        Returns None if status is not terminal or not a close reason.

        Args:
            status: Signal status

        Returns:
            Close reason string or None
        """
        return SignalStateMachine.CLOSE_REASONS.get(status)

    @staticmethod
    def get_valid_next_states(current: SignalStatus) -> Set[SignalStatus]:
        """
        Get all valid next states from current status.

        Useful for UI/logging to show what states are reachable.

        Args:
            current: Current signal status

        Returns:
            Set of valid next statuses
        """
        return SignalStateMachine.VALID_TRANSITIONS.get(current, set()).copy()

    @staticmethod
    def get_tp_progression_count(status: SignalStatus) -> int:
        """
        Get which take-profit level has been hit.

        Returns:
            0 for PENDING/ACTIVE, 1 for TP1_HIT, 2 for TP2_HIT, 3 for TP3_HIT
        """
        tp_mapping = {
            SignalStatus.PENDING: 0,
            SignalStatus.ACTIVE: 0,
            SignalStatus.TP1_HIT: 1,
            SignalStatus.TP2_HIT: 2,
            SignalStatus.TP3_HIT: 3,
            SignalStatus.SL_HIT: 0,
            SignalStatus.CLOSED: 0,
            SignalStatus.INVALID: 0,
        }
        return tp_mapping.get(status, 0)

    @staticmethod
    def get_status_display_name(status: SignalStatus) -> str:
        """
        Get a human-readable name for a status.

        Args:
            status: Signal status

        Returns:
            Display name
        """
        display_names = {
            SignalStatus.PENDING: "Pending Entry",
            SignalStatus.ACTIVE: "Active (Entry Hit)",
            SignalStatus.TP1_HIT: "TP1 Hit",
            SignalStatus.TP2_HIT: "TP2 Hit",
            SignalStatus.TP3_HIT: "TP3 Hit (All Targets)",
            SignalStatus.SL_HIT: "Stop-Loss Hit (Loss)",
            SignalStatus.CLOSED: "Closed",
            SignalStatus.INVALID: "Invalid",
        }
        return display_names.get(status, status.value)

    @staticmethod
    def get_status_category(status: SignalStatus) -> str:
        """
        Categorize a status as OPEN, WON, LOST, or OTHER.

        Useful for grouping signals in reports and analytics.

        Args:
            status: Signal status

        Returns:
            Category string: 'OPEN', 'WON', 'LOST', or 'OTHER'
        """
        if status in (SignalStatus.PENDING, SignalStatus.ACTIVE, SignalStatus.TP1_HIT, SignalStatus.TP2_HIT):
            return "OPEN"
        elif status in (SignalStatus.TP3_HIT, SignalStatus.CLOSED):
            return "WON"
        elif status == SignalStatus.SL_HIT:
            return "LOST"
        else:  # INVALID
            return "OTHER"

    @staticmethod
    def simulate_transitions(
        start_status: SignalStatus,
        events: list[EventType],
    ) -> list[TransitionResult]:
        """
        Simulate a sequence of events and return all transitions.

        Useful for debugging state machine logic and generating state
        diagrams. Stops immediately if an invalid transition is encountered.

        Args:
            start_status: Initial status
            events: List of events to process

        Returns:
            List of TransitionResult objects, one per event
        """
        results: list[TransitionResult] = []
        current = start_status

        for event in events:
            result = SignalStateMachine.process_event(current, event)
            results.append(result)

            # Update current status for next iteration
            current = result.new_status

            # Stop if we hit a terminal state
            if result.is_terminal and result.did_transition:
                break

        return results

    @staticmethod
    def get_all_valid_paths(
        start: SignalStatus,
        max_depth: int = 10,
    ) -> list[list[SignalStatus]]:
        """
        Get all possible valid state paths from a starting status.

        Useful for testing and understanding state machine completeness.
        Uses depth-first search to find all reachable states.

        Args:
            start: Starting status
            max_depth: Maximum search depth (prevent infinite loops)

        Returns:
            List of complete paths (each path is a list of statuses)
        """
        paths: list[list[SignalStatus]] = []

        def dfs(current: SignalStatus, path: list[SignalStatus], depth: int):
            """Depth-first search for valid paths."""
            if depth > max_depth:
                return

            # Add current path to results
            paths.append(path.copy())

            # If terminal, stop exploring this path
            if SignalStateMachine.is_terminal(current):
                return

            # Explore all valid next states
            next_states = SignalStateMachine.get_valid_next_states(current)
            for next_state in next_states:
                dfs(next_state, path + [next_state], depth + 1)

        dfs(start, [start], 0)
        return paths

    @staticmethod
    def build_state_diagram_data() -> Dict[str, any]:
        """
        Build data structure suitable for rendering a state diagram.

        Returns:
            Dictionary with nodes and edges for visualization
        """
        nodes = [
            {"id": status.value, "label": SignalStateMachine.get_status_display_name(status)}
            for status in SignalStatus
        ]

        edges = []
        for current, targets in SignalStateMachine.VALID_TRANSITIONS.items():
            for target in targets:
                edges.append({"from": current.value, "to": target.value})

        return {
            "nodes": nodes,
            "edges": edges,
            "terminal_states": [s.value for s in SignalStateMachine.TERMINAL_STATES],
        }
