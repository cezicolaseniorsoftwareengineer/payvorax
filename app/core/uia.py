"""
Architecture decision support — BioCodeTechPay.

Evaluates project requirements against known architectural trade-offs
and returns a structured recommendation. No ML, no GNN, no vector DB.
Decisions are deterministic and based on explicit rules, not simulated confidence.
"""
from dataclasses import dataclass
from enum import Enum
from typing import Dict, List


class ArchitectureType(str, Enum):
    MONOLITH = "monolith"
    MICROSERVICES = "microservices"
    SERVERLESS = "serverless"
    EMBEDDED = "embedded"


@dataclass
class ProjectRequirements:
    scale: str          # "global" | "regional" | "local"
    complexity: str     # "high" | "medium" | "low"
    legacy: bool
    embedded: bool
    compliance: List[str]


@dataclass
class ArchitectureDecision:
    architecture: ArchitectureType
    optimizations: List[str]
    trade_offs: Dict[str, str]
    rationale: str


def recommend_architecture(req: ProjectRequirements) -> ArchitectureDecision:
    """
    Returns a deterministic architecture recommendation based on project requirements.
    Rules derived from ADR-001 (hexagonal architecture decision record).
    """
    if req.embedded:
        return ArchitectureDecision(
            architecture=ArchitectureType.EMBEDDED,
            optimizations=["low_memory_footprint", "deterministic_execution"],
            trade_offs={
                "latency": "microseconds — no GC pauses",
                "flexibility": "low — hardware-bound",
            },
            rationale="Embedded constraint eliminates JVM/Python runtimes.",
        )

    if req.legacy:
        return ArchitectureDecision(
            architecture=ArchitectureType.MONOLITH,
            optimizations=["incremental_strangler_fig", "cqrs_at_boundary"],
            trade_offs={
                "deployment": "single unit — lower ops complexity",
                "scalability": "vertical only until extracted",
            },
            rationale="Legacy codebase: monolith reduces migration risk; extract bounded contexts incrementally.",
        )

    if req.scale == "global" and req.complexity == "high":
        return ArchitectureDecision(
            architecture=ArchitectureType.MICROSERVICES,
            optimizations=["event_sourcing", "saga_pattern", "distributed_tracing"],
            trade_offs={
                "latency": "network hops between services",
                "ops_complexity": "high — requires service mesh and observability platform",
                "team_size": "requires independent domain teams",
            },
            rationale="Global scale + high complexity justify distribution cost. Compliance isolation per bounded context.",
        )

    return ArchitectureDecision(
        architecture=ArchitectureType.SERVERLESS,
        optimizations=["fast_deployment", "cost_per_invocation"],
        trade_offs={
            "cold_start": "latency spikes on low-traffic paths",
            "state": "external state required — Redis or DB",
        },
        rationale="Local/regional scale with medium complexity: serverless reduces operational burden without distribution cost.",
    )
