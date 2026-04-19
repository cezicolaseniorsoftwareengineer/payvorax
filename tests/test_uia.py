import pytest
from app.core.uia import UltraIntelligenceAdaptative, ProjectRequirements, ArchitectureType


class TestUltraIntelligenceAdaptative:
    """
    Testes para Ultra Inteligência Adaptativa (UIA).
    Valida GNN, Meta-Learning, Vector DB, Adaptive Synthesis, Squad Simulation e Global Loop.
    """

    @pytest.fixture
    def uia_instance(self):
        return UltraIntelligenceAdaptative()

    @pytest.mark.asyncio
    async def test_analyze_dependencies(self, uia_instance):
        """Testa análise de dependências via GNN."""
        codebase = {
            "app/main.py": "import fastapi\nfrom app.core import config",
            "app/core/config.py": "import pydantic"
        }
        result = await uia_instance.analyze_dependencies(codebase)
        assert "nodes" in result
        assert result["nodes"] == 2
        assert "edges" in result

    @pytest.mark.asyncio
    async def test_meta_learn_patterns(self, uia_instance):
        """Testa meta-learning para auto-otimização."""
        task = "implement authentication"
        context = {"complexity": "high"}
        result = await uia_instance.meta_learn_patterns(task, context)
        assert "prediction" in result
        assert "confidence" in result
        assert 0 <= result["confidence"] <= 1

    @pytest.mark.asyncio
    async def test_optimize_persistence(self, uia_instance):
        """Testa otimização de persistência com vector DB."""
        data = b"sample data for compression"
        compressed = await uia_instance.optimize_persistence(data)
        assert len(compressed) <= len(data)  # Deve comprimir ou manter

    @pytest.mark.asyncio
    async def test_synthesize_architecture(self, uia_instance):
        """Testa síntese adaptativa de arquitetura."""
        req = ProjectRequirements(
            scale="global",
            complexity="high",
            legacy=False,
            embedded=False,
            compliance=["PCI DSS"]
        )
        decision = await uia_instance.synthesize_architecture(req)
        assert isinstance(decision.architecture, ArchitectureType)
        assert decision.confidence > 0
        assert "trade_offs" in decision.__dict__

    @pytest.mark.asyncio
    async def test_simulate_squad(self, uia_instance):
        """Testa simulação de squads invisíveis."""
        tasks = ["backend", "frontend"]
        result = await uia_instance.simulate_squad(tasks)
        assert "results" in result
        assert "consensus" in result
        assert len(result["results"]) == 2

    @pytest.mark.asyncio
    async def test_global_optimization_loop(self, uia_instance):
        """Testa loop de otimização global."""
        metrics = {"performance": 0.8, "security": 0.9, "cost": 0.7}
        result = await uia_instance.global_optimization_loop(metrics)
        assert "performance" in result
        assert "security" in result
        assert "cost" in result
