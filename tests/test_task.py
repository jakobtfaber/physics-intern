"""Tests for Task dataclass and TaskType enum."""

from sciralph.task import Task, TaskType, TASK_TYPE_AGENT_MAP


class TestTaskType:
    def test_values(self):
        assert TaskType.RESEARCH_EXPLORE == "research_explore"
        assert TaskType.COMPUTE_VERIFY == "compute_verify"
        assert TaskType.TERMINATE == "terminate"

    def test_from_string(self):
        assert TaskType("compute_verify") == TaskType.COMPUTE_VERIFY
        assert TaskType("research_explore") == TaskType.RESEARCH_EXPLORE


class TestTaskToMarkdown:
    def test_basic(self):
        task = Task(
            task_id="TASK-005",
            task_type=TaskType.COMPUTE_VERIFY,
            assigned_to="compute_verify",
            priority="high",
            iteration=5,
            body="# Task\n\nVerify result B.",
        )
        md = task.to_markdown()
        assert "task_id: TASK-005" in md
        assert "task_type: compute_verify" in md
        assert "assigned_to: compute_verify" in md
        assert "Verify result B." in md

    def test_with_blocking_critiques(self):
        task = Task(
            task_id="TASK-010",
            task_type=TaskType.RESEARCH_EXPLORE,
            assigned_to="research_explore",
            iteration=10,
            blocking_critiques=["CRIT-001", "CRIT-002"],
            body="Resolve critiques.",
        )
        md = task.to_markdown()
        assert "blocking_critiques" in md
        assert "CRIT-001" in md


class TestTaskFromFrontmatter:
    def test_basic(self):
        text = (
            "---\n"
            "task_id: TASK-003\n"
            "task_type: compute_verify\n"
            "assigned_to: compute_verify\n"
            "priority: high\n"
            "iteration: 3\n"
            "---\n\n"
            "Verify something."
        )
        task = Task.from_frontmatter(text)
        assert task.task_id == "TASK-003"
        assert task.task_type == TaskType.COMPUTE_VERIFY
        assert task.assigned_to == "compute_verify"
        assert task.iteration == 3
        assert "Verify something." in task.body

    def test_fallback_iteration(self):
        text = "---\ntask_type: research_explore\nassigned_to: research_explore\n---\n\nDo something."
        task = Task.from_frontmatter(text, fallback_iteration=7)
        assert task.task_id == "TASK-007"
        assert task.iteration == 7

    def test_unknown_task_type_defaults_to_research_explore(self):
        text = "---\ntask_type: foobar\nassigned_to: research_explore\n---\n\nBody."
        task = Task.from_frontmatter(text)
        assert task.task_type == TaskType.RESEARCH_EXPLORE

    def test_round_trip(self):
        original = Task(
            task_id="TASK-042",
            task_type=TaskType.CRITIQUE,
            assigned_to="deep_critic",
            priority="high",
            iteration=42,
            body="Review all claims.",
        )
        md = original.to_markdown()
        restored = Task.from_frontmatter(md)
        assert restored.task_id == original.task_id
        assert restored.task_type == original.task_type
        assert restored.assigned_to == original.assigned_to
        assert restored.iteration == original.iteration
        assert "Review all claims." in restored.body


class TestTaskFromFrontmatterEdgeCases:
    """Tests for Task.from_frontmatter edge cases (Improvement 6C)."""

    def test_from_frontmatter_empty_assigned_to(self):
        """Empty string assigned_to defaults to 'research_explore'."""
        text = "---\ntask_type: compute_verify\nassigned_to: ''\niteration: 5\n---\n\nBody."
        task = Task.from_frontmatter(text)
        assert task.assigned_to == "research_explore"

    def test_from_frontmatter_null_assigned_to(self):
        """null/None assigned_to defaults to 'research_explore'."""
        text = "---\ntask_type: compute_verify\nassigned_to:\niteration: 5\n---\n\nBody."
        task = Task.from_frontmatter(text)
        assert task.assigned_to == "research_explore"


class TestTaskTypeAgentMap:
    """Tests for TASK_TYPE_AGENT_MAP (Improvement 6A)."""

    def test_all_task_types_mapped(self):
        for tt in TaskType:
            assert tt in TASK_TYPE_AGENT_MAP, f"TaskType.{tt} not in TASK_TYPE_AGENT_MAP"

    def test_compute_verify_maps_to_compute_verify(self):
        assert TASK_TYPE_AGENT_MAP[TaskType.COMPUTE_VERIFY] == "compute_verify"

    def test_critique_maps_to_deep_critic(self):
        assert TASK_TYPE_AGENT_MAP[TaskType.CRITIQUE] == "deep_critic"

    def test_research_explore_maps_to_research_explore(self):
        assert TASK_TYPE_AGENT_MAP[TaskType.RESEARCH_EXPLORE] == "research_explore"


class TestStrategizeTaskType:
    def test_strategize_task_type_exists(self):
        assert TaskType.STRATEGIZE == "strategize"
        assert TaskType("strategize") == TaskType.STRATEGIZE

    def test_strategize_in_agent_map(self):
        assert TaskType.STRATEGIZE in TASK_TYPE_AGENT_MAP
        assert TASK_TYPE_AGENT_MAP[TaskType.STRATEGIZE] == "strategist"


class TestTaskTargetClaim:
    """Tests for target_claim field on Task (Phase 2: COMP→WH registry)."""

    def test_target_claim_in_markdown(self):
        task = Task(
            task_id="TASK-005", task_type=TaskType.COMPUTE_VERIFY,
            assigned_to="compute_verify", target_claim="WH-001",
            body="Verify WH-001.",
        )
        md = task.to_markdown()
        assert "target_claim: WH-001" in md

    def test_target_claim_omitted_when_empty(self):
        task = Task(
            task_id="TASK-005", task_type=TaskType.COMPUTE_VERIFY,
            assigned_to="compute_verify",
            body="Verify something.",
        )
        md = task.to_markdown()
        assert "target_claim" not in md

    def test_target_claim_round_trip(self):
        task = Task(
            task_id="TASK-005", task_type=TaskType.COMPUTE_VERIFY,
            assigned_to="compute_verify", target_claim="ER-003",
            body="Verify ER-003.",
        )
        restored = Task.from_frontmatter(task.to_markdown())
        assert restored.target_claim == "ER-003"

    def test_target_claim_missing_in_frontmatter(self):
        text = "---\ntask_type: compute_verify\nassigned_to: compute_verify\n---\n\nBody."
        task = Task.from_frontmatter(text)
        assert task.target_claim == ""
