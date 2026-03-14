"""Tests for config loading, YAML merge, and CLI parsers."""

import tempfile
import warnings
from argparse import Namespace
from pathlib import Path

import pytest
import yaml

from sciralph.config import Config, DEFAULTS, _YAML_CONFIG_FIELDS, load_config_yaml, build_config


# ---------------------------------------------------------------------------
# DEFAULTS loaded from config.default.yaml
# ---------------------------------------------------------------------------

class TestDefaults:
    def test_defaults_has_required_keys(self):
        required = {"model", "verify_model", "max_tokens", "max_iterations",
                     "critic_every_n", "compress_threshold",
                     "sympy_timeout_seconds",
                     "max_tool_rounds", "tool_output_limit", "min_er_for_completion"}
        assert required.issubset(DEFAULTS.keys())

    def test_defaults_model_is_string(self):
        assert isinstance(DEFAULTS["model"], str) and DEFAULTS["model"]

    def test_defaults_verify_model_is_string(self):
        assert isinstance(DEFAULTS["verify_model"], str) and DEFAULTS["verify_model"]

    def test_stall_recompute_limit_default(self):
        assert Config().stall_recompute_limit == 2

    def test_text_checkpoint_interval_default(self):
        assert Config().text_checkpoint_interval == 2
        assert DEFAULTS["text_checkpoint_interval"] == 2

    def test_text_checkpoint_interval_in_yaml_fields(self):
        assert "text_checkpoint_interval" in _YAML_CONFIG_FIELDS


# ---------------------------------------------------------------------------
# load_config_yaml
# ---------------------------------------------------------------------------

class TestLoadConfigYaml:
    def test_valid_file(self, tmp_path):
        cfg = tmp_path / "config.yaml"
        cfg.write_text(yaml.dump({"model": "custom-model", "max_iterations": 10}))
        result = load_config_yaml(cfg)
        assert result == {"model": "custom-model", "max_iterations": 10}

    def test_unknown_keys_warn(self, tmp_path):
        cfg = tmp_path / "config.yaml"
        cfg.write_text(yaml.dump({"model": "x", "bogus_key": 42}))
        with warnings.catch_warnings(record=True) as w:
            warnings.simplefilter("always")
            result = load_config_yaml(cfg)
            assert len(w) == 1
            assert "bogus_key" in str(w[0].message)
        assert "bogus_key" not in result
        assert result["model"] == "x"

    def test_empty_file(self, tmp_path):
        cfg = tmp_path / "config.yaml"
        cfg.write_text("")
        assert load_config_yaml(cfg) == {}

    def test_compress_threshold_dict(self, tmp_path):
        cfg = tmp_path / "config.yaml"
        threshold = {"RESEARCH_STATE.md": 10000}
        cfg.write_text(yaml.dump({"compress_threshold": threshold}))
        result = load_config_yaml(cfg)
        assert result["compress_threshold"] == threshold

    def test_excluded_fields_rejected(self, tmp_path):
        cfg = tmp_path / "config.yaml"
        cfg.write_text(yaml.dump({"workspace_dir": "/tmp/ws", "api_key": "secret"}))
        with warnings.catch_warnings(record=True) as w:
            warnings.simplefilter("always")
            result = load_config_yaml(cfg)
            assert len(w) == 2
        assert "workspace_dir" not in result
        assert "api_key" not in result

    def test_verify_model_accepted(self, tmp_path):
        cfg = tmp_path / "config.yaml"
        cfg.write_text(yaml.dump({"verify_model": "some-model"}))
        result = load_config_yaml(cfg)
        assert result["verify_model"] == "some-model"


# ---------------------------------------------------------------------------
# build_config
# ---------------------------------------------------------------------------

class TestBuildConfig:
    def test_defaults_only(self):
        args = Namespace(config=None, model=None, max_tokens=None,
                         max_iterations=None, workspace_dir=None,
                         critic_every_n=None, sympy_timeout_seconds=None)
        cfg = build_config(args)
        assert cfg.model == DEFAULTS["model"]
        assert cfg.verify_model == DEFAULTS["verify_model"]
        assert cfg.max_iterations == DEFAULTS["max_iterations"]
        assert cfg.max_tokens == DEFAULTS["max_tokens"]

    def test_cli_overrides_defaults(self):
        args = Namespace(config=None, model="cli-model",
                         max_tokens=None, max_iterations=5,
                         workspace_dir=None, critic_every_n=None,
                         sympy_timeout_seconds=None)
        cfg = build_config(args)
        assert cfg.model == "cli-model"
        assert cfg.max_iterations == 5
        assert cfg.max_tokens == DEFAULTS["max_tokens"]  # default preserved

    def test_yaml_overrides_defaults(self, tmp_path):
        cfg_file = tmp_path / "config.yaml"
        cfg_file.write_text(yaml.dump({"model": "custom-model", "max_iterations": 50}))
        args = Namespace(config=str(cfg_file), model=None, max_tokens=None,
                         max_iterations=None, workspace_dir=None,
                         critic_every_n=None, sympy_timeout_seconds=None)
        cfg = build_config(args)
        assert cfg.model == "custom-model"
        assert cfg.max_iterations == 50

    def test_cli_overrides_yaml(self, tmp_path):
        cfg_file = tmp_path / "config.yaml"
        cfg_file.write_text(yaml.dump({"model": "yaml-model", "max_iterations": 50}))
        args = Namespace(config=str(cfg_file), model="cli-model", max_tokens=None,
                         max_iterations=None, workspace_dir=None,
                         critic_every_n=None, sympy_timeout_seconds=None)
        cfg = build_config(args)
        assert cfg.model == "cli-model"
        assert cfg.max_iterations == 50  # from YAML

    def test_partial_overlap(self, tmp_path):
        cfg_file = tmp_path / "config.yaml"
        cfg_file.write_text(yaml.dump({"max_tokens": 8192, "critic_every_n": 2}))
        args = Namespace(config=str(cfg_file), model=None, max_tokens=None,
                         max_iterations=10, workspace_dir=None,
                         critic_every_n=None, sympy_timeout_seconds=None)
        cfg = build_config(args)
        assert cfg.max_tokens == 8192  # from YAML
        assert cfg.critic_every_n == 2  # from YAML
        assert cfg.max_iterations == 10  # from CLI


# ---------------------------------------------------------------------------
# main.py parser
# ---------------------------------------------------------------------------

class TestMainParser:
    def test_basic_args(self):
        from sciralph.main import build_parser
        parser = build_parser()
        args = parser.parse_args(["problems/test.yaml", "--max-iterations", "5",
                                  "--model", "my-model"])
        assert args.problem == Path("problems/test.yaml")
        assert args.max_iterations == 5
        assert args.model == "my-model"
        assert args.config is None

    def test_all_args(self):
        from sciralph.main import build_parser
        parser = build_parser()
        args = parser.parse_args([
            "p.yaml", "--config", "c.yaml", "--model", "m",
            "--max-tokens", "1024", "--max-iterations", "3",
            "--workspace-dir", "/tmp/ws", "--critic-every-n", "2",
            "--sympy-timeout-seconds", "30",
        ])
        assert args.config == Path("c.yaml")
        assert args.max_tokens == 1024
        assert args.workspace_dir == "/tmp/ws"
        assert args.critic_every_n == 2
        assert args.sympy_timeout_seconds == 30

    def test_bad_int_exits(self):
        from sciralph.main import build_parser
        parser = build_parser()
        with pytest.raises(SystemExit):
            parser.parse_args(["p.yaml", "--max-iterations", "abc"])

    def test_defaults_are_none(self):
        from sciralph.main import build_parser
        parser = build_parser()
        args = parser.parse_args(["p.yaml"])
        assert args.model is None
        assert args.max_tokens is None
        assert args.max_iterations is None
        assert args.workspace_dir is None
        assert args.critic_every_n is None
        assert args.sympy_timeout_seconds is None


# ---------------------------------------------------------------------------
# verify.py parser
# ---------------------------------------------------------------------------

class TestVerifyParser:
    def test_basic_args(self):
        from sciralph.verify import build_verify_parser
        parser = build_verify_parser()
        args = parser.parse_args(["workspaces/run1"])
        assert args.workspace_dir == "workspaces/run1"
        assert args.model == DEFAULTS["verify_model"]
        assert args.max_tokens == DEFAULTS["max_tokens"]
        assert args.timeout == 60
        assert args.rerun_computations is False
        assert args.write_report is False

    def test_boolean_flags(self):
        from sciralph.verify import build_verify_parser
        parser = build_verify_parser()
        args = parser.parse_args(["ws", "--rerun-computations", "--write-report"])
        assert args.rerun_computations is True
        assert args.write_report is True

    def test_custom_values(self):
        from sciralph.verify import build_verify_parser
        parser = build_verify_parser()
        args = parser.parse_args(["ws", "--model", "opus", "--max-tokens", "8192",
                                  "--timeout", "120"])
        assert args.model == "opus"
        assert args.max_tokens == 8192
        assert args.timeout == 120

    def test_bad_int_exits(self):
        from sciralph.verify import build_verify_parser
        parser = build_verify_parser()
        with pytest.raises(SystemExit):
            parser.parse_args(["ws", "--max-tokens", "xyz"])