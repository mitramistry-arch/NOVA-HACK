"""Neutral conversational message templates and slot pools for filler generation."""

import random

TEMPLATES: list[str] = [
    "Has anyone checked the {metric} on {environment} today?",
    "The latest {artifact} has been pushed to {environment}.",
    "Can someone review the PR for {component} {timeframe}?",
    "We need to schedule the {meeting_type} for {day} at {time}.",
    "The {component} tests are currently {status}.",
    "Please make sure the {doc_topic} are updated {timeframe}.",
    "I noticed high {metric} during the performance smoke test.",
    "The {meeting_type} has been rescheduled to {time} on {day}.",
    "Let's hold off on deploying to {environment} until {timeframe}.",
    "I finished {task} across the repository.",
    "Who is the point of contact for {component} this week?",
    "The CI run for the latest {component} PR finished successfully.",
    "Remember to submit feedback on the {doc_topic} by {day}.",
    "We saw a temporary spike in {metric} earlier today.",
    "All tickets for {meeting_type} have been prioritized.",
    "Is anyone currently working on {task}?",
    "The smoke tests passed on {environment} without issues.",
    "Could you share the link to the {doc_topic}?",
    "Deploying the updated {artifact} to {environment} now.",
    "Quick reminder about our {meeting_type} starting at {time}.",
    "The PR updating the {component} is ready for a second look.",
    "Let's track the {metric} closely after today's deploy.",
    "I am spending the afternoon on {task}.",
    "Can we verify if {environment} is ready for tomorrow's demo?",
    "The draft for the {doc_topic} is open for comments.",
    "Looks like the {component} changes resolved the earlier warning.",
    "Let me know if the {meeting_type} conflicts with your schedule.",
    "We should run a load test on {environment} {timeframe}.",
    "The team made good progress on {task} today.",
    "Please check if your local branch has the newest {artifact}.",
    "The {metric} stabilized following the configuration update.",
    "I will be offline for thirty minutes during lunch.",
    "Let's regroup after the {meeting_type} to discuss next steps.",
    "Is the documentation for {component} up to date?",
    "We are seeing consistent {metric} across all nodes.",
    "Just merged the cleanup PR for {task}.",
    "Does {environment} require any special flags for this run?",
    "I will prepare the slides for the {meeting_type} on {day}.",
    "The rollback on {environment} completed cleanly.",
    "We should review the latest {artifact} before releasing.",
    "Anyone having issues accessing {environment} this morning?",
    "Thanks for the thorough review on the {component} changes.",
    "The {doc_topic} should be reviewed before next sprint.",
    "Observability alerts for {metric} have been reset to normal.",
    "Let's discuss this during our weekly {meeting_type}.",
    "I created a follow-up ticket for {task}.",
    "The staging build of {artifact} is available for QA.",
    "Checking the health endpoints on {environment} now.",
    "Let me know when the {component} review is done.",
    "The overall {metric} looks healthy across the board.",
    "Don't forget to push your WIP branches before the weekend.",
    "The new version of {artifact} resolved the build warning.",
    "Can you confirm the attendees for {meeting_type} on {day}?",
    "Working on {task} to improve local dev speed.",
    "We should verify the {metric} threshold on {environment}.",
    "Please tag me on any pull requests affecting {component}.",
    "I will send out meeting notes following the {meeting_type}.",
    "Everything looks green on {environment} right now.",
    "Let's review the updated {doc_topic} together tomorrow.",
    "The nightly test suite for {component} ran cleanly.",
]

SLOT_POOLS: dict[str, list[str]] = {
    "metric": [
        "CPU load",
        "memory consumption",
        "request latency",
        "p99 response time",
        "queue depth",
        "disk throughput",
        "network bandwidth",
        "error count",
    ],
    "environment": [
        "staging",
        "sandbox",
        "dev cluster",
        "testbed",
        "qa environment",
        "preprod",
    ],
    "artifact": [
        "docker image",
        "release package",
        "client bundle",
        "tarball",
        "binary build",
    ],
    "component": [
        "auth middleware",
        "structured logger",
        "connection pool",
        "event loop",
        "rate limiter",
        "metrics collector",
        "task runner",
        "retry handler",
    ],
    "timeframe": [
        "this afternoon",
        "tomorrow morning",
        "before end of day",
        "next Monday",
        "after sprint sync",
        "by end of week",
    ],
    "day": [
        "Monday",
        "Tuesday",
        "Wednesday",
        "Thursday",
        "Friday",
    ],
    "time": [
        "10:00 AM",
        "11:30 AM",
        "2:00 PM",
        "3:30 PM",
        "4:15 PM",
    ],
    "meeting_type": [
        "standup",
        "sprint review",
        "backlog grooming",
        "tech retrospective",
        "architecture sync",
    ],
    "status": [
        "passing",
        "green",
        "running cleanly",
        "in progress",
        "stable",
    ],
    "doc_topic": [
        "onboarding instructions",
        "style guidelines",
        "release checklist",
        "troubleshooting guide",
        "deployment steps",
    ],
    "task": [
        "cleaning up unused imports",
        "formatting docstrings",
        "updating dependencies",
        "refactoring helper utilities",
        "organizing test fixtures",
    ],
}


def generate_filler(
    rng: random.Random,
    forbidden_values: set[str] | None = None,
) -> str:
    """Generate a single neutral conversational message from combinatorial slots."""
    forbidden = {v.lower().strip() for v in (forbidden_values or set()) if v.strip()}
    for _ in range(100):
        template = rng.choice(TEMPLATES)
        slots = {k: rng.choice(v) for k, v in SLOT_POOLS.items()}
        msg = template.format(**slots)
        msg_lower = msg.lower()
        if not any(f in msg_lower for f in forbidden):
            return msg
    return "Status check looks normal across all services."
