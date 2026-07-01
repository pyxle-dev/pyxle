"""Tests for CORS and CSRF configuration parsing in pyxle.config."""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from pyxle.config import (
    CacheConfig,
    ConfigError,
    CorsConfig,
    CsrfConfig,
    LlmsConfig,
    ObservabilityConfig,
    PyxleConfig,
    load_config,
)


# ---------------------------------------------------------------------------
# CorsConfig defaults
# ---------------------------------------------------------------------------


class TestCorsConfigDefaults:
    def test_default_not_enabled(self):
        config = CorsConfig()
        assert not config.enabled

    def test_enabled_when_origins_set(self):
        config = CorsConfig(origins=("http://localhost:3000",))
        assert config.enabled

    def test_default_methods(self):
        config = CorsConfig()
        assert "GET" in config.methods
        assert "POST" in config.methods

    def test_default_max_age(self):
        assert CorsConfig().max_age == 600


# ---------------------------------------------------------------------------
# CsrfConfig defaults
# ---------------------------------------------------------------------------


class TestCsrfConfigDefaults:
    def test_default_enabled(self):
        assert CsrfConfig().enabled is True

    def test_default_cookie_name(self):
        assert CsrfConfig().cookie_name == "pyxle-csrf"

    def test_default_samesite(self):
        assert CsrfConfig().cookie_samesite == "lax"


# ---------------------------------------------------------------------------
# Config JSON parsing — CORS
# ---------------------------------------------------------------------------


class TestCorsConfigParsing:
    def _load(self, tmp_path: Path, cors_data: dict) -> PyxleConfig:
        config_file = tmp_path / "pyxle.config.json"
        config_file.write_text(json.dumps({"cors": cors_data}))
        return load_config(tmp_path, config_path=config_file)

    def test_basic_origins(self, tmp_path: Path):
        config = self._load(tmp_path, {"origins": ["http://localhost:3000"]})
        assert config.cors.enabled
        assert config.cors.origins == ("http://localhost:3000",)

    def test_multiple_origins(self, tmp_path: Path):
        config = self._load(tmp_path, {
            "origins": ["http://localhost:3000", "https://example.com"]
        })
        assert len(config.cors.origins) == 2

    def test_custom_methods(self, tmp_path: Path):
        config = self._load(tmp_path, {
            "origins": ["*"],
            "methods": ["GET", "POST"],
        })
        assert config.cors.methods == ("GET", "POST")

    def test_credentials_flag(self, tmp_path: Path):
        config = self._load(tmp_path, {
            "origins": ["*"],
            "credentials": True,
        })
        assert config.cors.credentials is True

    def test_custom_max_age(self, tmp_path: Path):
        config = self._load(tmp_path, {
            "origins": ["*"],
            "maxAge": 3600,
        })
        assert config.cors.max_age == 3600

    def test_custom_headers(self, tmp_path: Path):
        config = self._load(tmp_path, {
            "origins": ["*"],
            "headers": ["Authorization", "Content-Type"],
        })
        assert config.cors.headers == ("Authorization", "Content-Type")

    def test_invalid_cors_type_raises(self, tmp_path: Path):
        config_file = tmp_path / "pyxle.config.json"
        config_file.write_text(json.dumps({"cors": "invalid"}))
        with pytest.raises(ConfigError, match="cors"):
            load_config(tmp_path, config_path=config_file)

    def test_invalid_credentials_type_raises(self, tmp_path: Path):
        config_file = tmp_path / "pyxle.config.json"
        config_file.write_text(json.dumps({"cors": {"credentials": "yes"}}))
        with pytest.raises(ConfigError, match="credentials"):
            load_config(tmp_path, config_path=config_file)

    def test_negative_max_age_raises(self, tmp_path: Path):
        config_file = tmp_path / "pyxle.config.json"
        config_file.write_text(json.dumps({"cors": {"maxAge": -1}}))
        with pytest.raises(ConfigError, match="maxAge"):
            load_config(tmp_path, config_path=config_file)

    def test_no_cors_block_returns_defaults(self, tmp_path: Path):
        config_file = tmp_path / "pyxle.config.json"
        config_file.write_text("{}")
        config = load_config(tmp_path, config_path=config_file)
        assert not config.cors.enabled

    def test_unknown_cors_key_raises(self, tmp_path: Path):
        config_file = tmp_path / "pyxle.config.json"
        config_file.write_text(json.dumps({"cors": {"orgins": ["*"]}}))
        with pytest.raises(ConfigError, match="Unknown keys in 'cors'"):
            load_config(tmp_path, config_path=config_file)


# ---------------------------------------------------------------------------
# Config JSON parsing — CSRF
# ---------------------------------------------------------------------------


class TestCsrfConfigParsing:
    def _load(self, tmp_path: Path, csrf_data) -> PyxleConfig:
        config_file = tmp_path / "pyxle.config.json"
        config_file.write_text(json.dumps({"csrf": csrf_data}))
        return load_config(tmp_path, config_path=config_file)

    def test_boolean_false_disables(self, tmp_path: Path):
        config = self._load(tmp_path, False)
        assert config.csrf.enabled is False

    def test_boolean_true_enables(self, tmp_path: Path):
        config = self._load(tmp_path, True)
        assert config.csrf.enabled is True

    def test_object_with_enabled_false(self, tmp_path: Path):
        config = self._load(tmp_path, {"enabled": False})
        assert config.csrf.enabled is False

    def test_custom_cookie_name(self, tmp_path: Path):
        config = self._load(tmp_path, {"cookieName": "my-csrf"})
        assert config.csrf.cookie_name == "my-csrf"

    def test_custom_header_name(self, tmp_path: Path):
        config = self._load(tmp_path, {"headerName": "x-my-token"})
        assert config.csrf.header_name == "x-my-token"

    def test_cookie_secure(self, tmp_path: Path):
        config = self._load(tmp_path, {"cookieSecure": True})
        assert config.csrf.cookie_secure is True

    def test_samesite_strict(self, tmp_path: Path):
        config = self._load(tmp_path, {"cookieSameSite": "strict"})
        assert config.csrf.cookie_samesite == "strict"

    def test_samesite_none(self, tmp_path: Path):
        config = self._load(tmp_path, {"cookieSameSite": "None"})
        assert config.csrf.cookie_samesite == "none"

    def test_invalid_samesite_raises(self, tmp_path: Path):
        config_file = tmp_path / "pyxle.config.json"
        config_file.write_text(json.dumps({"csrf": {"cookieSameSite": "invalid"}}))
        with pytest.raises(ConfigError, match="cookieSameSite"):
            load_config(tmp_path, config_path=config_file)

    def test_exempt_paths(self, tmp_path: Path):
        config = self._load(tmp_path, {"exemptPaths": ["/api/webhooks"]})
        assert config.csrf.exempt_paths == ("/api/webhooks",)

    def test_invalid_csrf_type_raises(self, tmp_path: Path):
        config_file = tmp_path / "pyxle.config.json"
        config_file.write_text(json.dumps({"csrf": 42}))
        with pytest.raises(ConfigError, match="csrf"):
            load_config(tmp_path, config_path=config_file)

    def test_invalid_enabled_type_raises(self, tmp_path: Path):
        config_file = tmp_path / "pyxle.config.json"
        config_file.write_text(json.dumps({"csrf": {"enabled": "yes"}}))
        with pytest.raises(ConfigError, match="csrf.enabled"):
            load_config(tmp_path, config_path=config_file)

    def test_empty_cookie_name_raises(self, tmp_path: Path):
        config_file = tmp_path / "pyxle.config.json"
        config_file.write_text(json.dumps({"csrf": {"cookieName": ""}}))
        with pytest.raises(ConfigError, match="cookieName"):
            load_config(tmp_path, config_path=config_file)

    def test_no_csrf_block_returns_defaults(self, tmp_path: Path):
        config_file = tmp_path / "pyxle.config.json"
        config_file.write_text("{}")
        config = load_config(tmp_path, config_path=config_file)
        assert config.csrf.enabled is True

    def test_unknown_csrf_key_raises(self, tmp_path: Path):
        config_file = tmp_path / "pyxle.config.json"
        config_file.write_text(json.dumps({"csrf": {"headerNme": "x"}}))
        with pytest.raises(ConfigError, match="Unknown keys in 'csrf'"):
            load_config(tmp_path, config_path=config_file)

    def test_miscased_same_site_typo_raises_not_silently_downgraded(self, tmp_path: Path):
        # The exact F3 regression: a mis-cased 'cookieSamesite' used to be
        # silently dropped, leaving SameSite=lax (a security downgrade). It must
        # now raise instead of quietly keeping the default.
        config_file = tmp_path / "pyxle.config.json"
        config_file.write_text(
            json.dumps({"csrf": {"cookieSamesite": "strict"}})
        )
        with pytest.raises(ConfigError, match="Unknown keys in 'csrf'"):
            load_config(tmp_path, config_path=config_file)


# ---------------------------------------------------------------------------
# Config passes CORS/CSRF to devserver kwargs
# ---------------------------------------------------------------------------


class TestDevserverKwargs:
    def test_to_devserver_kwargs_includes_cors(self):
        config = PyxleConfig(cors=CorsConfig(origins=("*",)))
        kwargs = config.to_devserver_kwargs()
        assert kwargs["cors"].origins == ("*",)

    def test_to_devserver_kwargs_includes_csrf(self):
        config = PyxleConfig(csrf=CsrfConfig(enabled=False))
        kwargs = config.to_devserver_kwargs()
        assert kwargs["csrf"].enabled is False

    def test_to_devserver_kwargs_includes_cache(self):
        config = PyxleConfig(cache=CacheConfig(routes=(("/", 60),)))
        kwargs = config.to_devserver_kwargs()
        assert kwargs["cache"].routes == (("/", 60),)


# ---------------------------------------------------------------------------
# CacheConfig defaults and route matching
# ---------------------------------------------------------------------------


class TestCacheConfigDefaults:
    def test_default_not_enabled(self):
        assert CacheConfig().enabled is False

    def test_enabled_when_routes_set(self):
        assert CacheConfig(routes=(("/", 60),)).enabled is True


class TestCacheConfigMatching:
    """``max_age_for`` decides which page responses become publicly
    cacheable. Its precedence rules (exact > longest-prefix wildcard) are
    security-relevant: a too-greedy match could mark a per-user page as
    shared-cacheable, so the boundaries get explicit coverage."""

    def test_exact_match(self):
        cache = CacheConfig(routes=(("/about", 120),))
        assert cache.max_age_for("/about") == 120

    def test_no_match_returns_none(self):
        cache = CacheConfig(routes=(("/about", 120),))
        assert cache.max_age_for("/contact") is None

    def test_wildcard_matches_bare_prefix(self):
        cache = CacheConfig(routes=(("/docs/*", 300),))
        # The bare prefix path (no trailing segment) also matches.
        assert cache.max_age_for("/docs") == 300

    def test_wildcard_matches_nested_path(self):
        cache = CacheConfig(routes=(("/docs/*", 300),))
        assert cache.max_age_for("/docs/guides/intro") == 300

    def test_wildcard_respects_path_boundary(self):
        """``/docs/*`` must NOT match ``/docsearch`` — a shared string
        prefix that isn't a path-segment boundary is a different route."""
        cache = CacheConfig(routes=(("/docs/*", 300),))
        assert cache.max_age_for("/docsearch") is None

    def test_longest_wildcard_prefix_wins(self):
        # Less-specific listed first so the ``score > best_score`` update
        # path is exercised when the more-specific rule overtakes it.
        cache = CacheConfig(routes=(("/docs/*", 100), ("/docs/api/*", 200)))
        assert cache.max_age_for("/docs/api/auth") == 200
        assert cache.max_age_for("/docs/guides") == 100

    def test_exact_beats_wildcard(self):
        cache = CacheConfig(routes=(("/docs/*", 100), ("/docs/api", 200)))
        assert cache.max_age_for("/docs/api") == 200

    def test_root_wildcard_is_catch_all(self):
        cache = CacheConfig(routes=(("/*", 30),))
        assert cache.max_age_for("/anything/here") == 30
        assert cache.max_age_for("/") == 30


# ---------------------------------------------------------------------------
# Config JSON parsing — cache
# ---------------------------------------------------------------------------


class TestCacheConfigParsing:
    def _load(self, tmp_path: Path, cache_data) -> PyxleConfig:
        config_file = tmp_path / "pyxle.config.json"
        config_file.write_text(json.dumps({"cache": cache_data}))
        return load_config(tmp_path, config_path=config_file)

    def test_integer_shorthand(self, tmp_path: Path):
        config = self._load(tmp_path, {"/": 60, "/docs/*": 300})
        assert config.cache.enabled
        assert config.cache.max_age_for("/") == 60
        assert config.cache.max_age_for("/docs/x") == 300

    def test_object_form_smaxage(self, tmp_path: Path):
        config = self._load(tmp_path, {"/": {"sMaxage": 120}})
        assert config.cache.max_age_for("/") == 120

    def test_object_form_maxage_fallback(self, tmp_path: Path):
        config = self._load(tmp_path, {"/": {"maxAge": 90}})
        assert config.cache.max_age_for("/") == 90

    def test_zero_max_age_allowed(self, tmp_path: Path):
        # s-maxage=0 (cache but revalidate immediately) is a valid policy.
        config = self._load(tmp_path, {"/": 0})
        assert config.cache.max_age_for("/") == 0

    def test_boolean_value_rejected(self, tmp_path: Path):
        config_file = tmp_path / "pyxle.config.json"
        config_file.write_text(json.dumps({"cache": {"/": True}}))
        with pytest.raises(ConfigError, match="boolean"):
            load_config(tmp_path, config_path=config_file)

    def test_negative_max_age_rejected(self, tmp_path: Path):
        config_file = tmp_path / "pyxle.config.json"
        config_file.write_text(json.dumps({"cache": {"/": -5}}))
        with pytest.raises(ConfigError, match="max-age"):
            load_config(tmp_path, config_path=config_file)

    def test_non_absolute_pattern_rejected(self, tmp_path: Path):
        config_file = tmp_path / "pyxle.config.json"
        config_file.write_text(json.dumps({"cache": {"docs": 60}}))
        with pytest.raises(ConfigError, match="absolute paths"):
            load_config(tmp_path, config_path=config_file)

    def test_non_object_cache_rejected(self, tmp_path: Path):
        config_file = tmp_path / "pyxle.config.json"
        config_file.write_text(json.dumps({"cache": "nope"}))
        with pytest.raises(ConfigError, match="cache"):
            load_config(tmp_path, config_path=config_file)

    def test_non_integer_smaxage_rejected(self, tmp_path: Path):
        config_file = tmp_path / "pyxle.config.json"
        config_file.write_text(json.dumps({"cache": {"/": {"sMaxage": "soon"}}}))
        with pytest.raises(ConfigError, match="sMaxage"):
            load_config(tmp_path, config_path=config_file)

    def test_object_without_smaxage_rejected(self, tmp_path: Path):
        config_file = tmp_path / "pyxle.config.json"
        config_file.write_text(json.dumps({"cache": {"/": {}}}))
        with pytest.raises(ConfigError, match="sMaxage"):
            load_config(tmp_path, config_path=config_file)

    def test_list_value_rejected(self, tmp_path: Path):
        config_file = tmp_path / "pyxle.config.json"
        config_file.write_text(json.dumps({"cache": {"/": [60]}}))
        with pytest.raises(ConfigError, match="cache route"):
            load_config(tmp_path, config_path=config_file)

    def test_no_cache_block_returns_defaults(self, tmp_path: Path):
        config_file = tmp_path / "pyxle.config.json"
        config_file.write_text("{}")
        config = load_config(tmp_path, config_path=config_file)
        assert config.cache.enabled is False


# ---------------------------------------------------------------------------
# ObservabilityConfig defaults + parsing
# ---------------------------------------------------------------------------


class TestObservabilityConfigDefaults:
    def test_request_id_on_by_default(self):
        assert ObservabilityConfig().request_id is True

    def test_timing_on_by_default(self):
        assert ObservabilityConfig().timing is True

    def test_does_not_trust_incoming_id_by_default(self):
        # Echoing client-supplied ids is a spoofing vector; off by default.
        assert ObservabilityConfig().trust_incoming_request_id is False

    def test_default_header_name(self):
        assert ObservabilityConfig().request_id_header == "X-Request-Id"

    def test_enabled_reflects_either_signal(self):
        assert ObservabilityConfig().enabled is True
        assert ObservabilityConfig(request_id=False, timing=False).enabled is False
        assert ObservabilityConfig(request_id=False, timing=True).enabled is True


class TestObservabilityConfigParsing:
    def _load(self, tmp_path: Path, observability_data) -> PyxleConfig:
        config_file = tmp_path / "pyxle.config.json"
        config_file.write_text(json.dumps({"observability": observability_data}))
        return load_config(tmp_path, config_path=config_file)

    def test_no_block_returns_defaults(self, tmp_path: Path):
        config_file = tmp_path / "pyxle.config.json"
        config_file.write_text("{}")
        config = load_config(tmp_path, config_path=config_file)
        assert config.observability == ObservabilityConfig()

    def test_bare_false_disables_both(self, tmp_path: Path):
        config = self._load(tmp_path, False)
        assert config.observability.request_id is False
        assert config.observability.timing is False

    def test_bare_true_enables_both(self, tmp_path: Path):
        config = self._load(tmp_path, True)
        assert config.observability.request_id is True
        assert config.observability.timing is True

    def test_object_fields(self, tmp_path: Path):
        config = self._load(
            tmp_path,
            {
                "requestId": False,
                "requestIdHeader": "X-Trace-Id",
                "trustIncomingRequestId": True,
                "timing": False,
            },
        )
        obs = config.observability
        assert obs.request_id is False
        assert obs.request_id_header == "X-Trace-Id"
        assert obs.trust_incoming_request_id is True
        assert obs.timing is False

    def test_threads_into_devserver_kwargs(self, tmp_path: Path):
        config = self._load(tmp_path, {"requestId": False})
        assert config.to_devserver_kwargs()["observability"] is config.observability

    def test_invalid_top_type_raises(self, tmp_path: Path):
        config_file = tmp_path / "pyxle.config.json"
        config_file.write_text(json.dumps({"observability": 42}))
        with pytest.raises(ConfigError, match="observability"):
            load_config(tmp_path, config_path=config_file)

    def test_unknown_observability_key_raises(self, tmp_path: Path):
        config_file = tmp_path / "pyxle.config.json"
        config_file.write_text(json.dumps({"observability": {"requestid": True}}))
        with pytest.raises(ConfigError, match="Unknown keys in 'observability'"):
            load_config(tmp_path, config_path=config_file)

    def test_invalid_request_id_type_raises(self, tmp_path: Path):
        with pytest.raises(ConfigError, match="observability.requestId"):
            self._load(tmp_path, {"requestId": "yes"})

    def test_empty_header_name_raises(self, tmp_path: Path):
        with pytest.raises(ConfigError, match="requestIdHeader"):
            self._load(tmp_path, {"requestIdHeader": "  "})

    def test_invalid_trust_incoming_type_raises(self, tmp_path: Path):
        with pytest.raises(ConfigError, match="trustIncomingRequestId"):
            self._load(tmp_path, {"trustIncomingRequestId": "no"})

    def test_invalid_timing_type_raises(self, tmp_path: Path):
        with pytest.raises(ConfigError, match="observability.timing"):
            self._load(tmp_path, {"timing": 1})

    def test_metrics_endpoint_defaults_off(self):
        obs = ObservabilityConfig()
        assert obs.metrics_endpoint is False
        assert obs.metrics_endpoint_path == "/api/__pyxle/metrics"
        assert obs.metrics_endpoint_token is None

    def test_metrics_endpoint_fields(self, tmp_path: Path):
        config = self._load(
            tmp_path,
            {
                "metricsEndpoint": True,
                "metricsEndpointPath": "/internal/metrics",
                "metricsEndpointToken": "s3cret",
            },
        )
        obs = config.observability
        assert obs.metrics_endpoint is True
        assert obs.metrics_endpoint_path == "/internal/metrics"
        assert obs.metrics_endpoint_token == "s3cret"

    def test_metrics_endpoint_enables_observability(self):
        # Even with request-id and timing off, the metrics endpoint counts as
        # "enabled" so the recording middleware is installed.
        obs = ObservabilityConfig(request_id=False, timing=False, metrics_endpoint=True)
        assert obs.enabled is True

    def test_invalid_metrics_endpoint_type_raises(self, tmp_path: Path):
        with pytest.raises(ConfigError, match="metricsEndpoint"):
            self._load(tmp_path, {"metricsEndpoint": "yes"})

    def test_relative_metrics_path_raises(self, tmp_path: Path):
        with pytest.raises(ConfigError, match="metricsEndpointPath"):
            self._load(tmp_path, {"metricsEndpointPath": "metrics"})

    def test_empty_metrics_token_raises(self, tmp_path: Path):
        with pytest.raises(ConfigError, match="metricsEndpointToken"):
            self._load(tmp_path, {"metricsEndpointToken": "  "})

    def test_access_log_defaults(self):
        obs = ObservabilityConfig()
        assert obs.access_log is False
        assert obs.log_format == "console"
        assert obs.log_level == "INFO"

    def test_access_log_fields(self, tmp_path: Path):
        config = self._load(
            tmp_path,
            {"accessLog": True, "logFormat": "json", "logLevel": "debug"},
        )
        obs = config.observability
        assert obs.access_log is True
        assert obs.log_format == "json"
        assert obs.log_level == "DEBUG"  # normalised to upper-case

    def test_access_log_enables_observability(self):
        obs = ObservabilityConfig(request_id=False, timing=False, access_log=True)
        assert obs.enabled is True

    def test_invalid_access_log_type_raises(self, tmp_path: Path):
        with pytest.raises(ConfigError, match="accessLog"):
            self._load(tmp_path, {"accessLog": "yes"})

    def test_invalid_log_format_raises(self, tmp_path: Path):
        with pytest.raises(ConfigError, match="logFormat"):
            self._load(tmp_path, {"logFormat": "xml"})

    def test_invalid_log_level_raises(self, tmp_path: Path):
        with pytest.raises(ConfigError, match="logLevel"):
            self._load(tmp_path, {"logLevel": "LOUD"})

    def test_otel_defaults(self):
        obs = ObservabilityConfig()
        assert obs.otel is False
        assert obs.otel_service_name == "pyxle-app"
        assert obs.otel_sample_ratio == 0.05

    def test_otel_fields(self, tmp_path: Path):
        config = self._load(
            tmp_path,
            {"otel": True, "otelServiceName": "shop", "otelSampleRatio": 0.5},
        )
        obs = config.observability
        assert obs.otel is True
        assert obs.otel_service_name == "shop"
        assert obs.otel_sample_ratio == 0.5

    def test_invalid_otel_type_raises(self, tmp_path: Path):
        with pytest.raises(ConfigError, match="observability.otel"):
            self._load(tmp_path, {"otel": "on"})

    def test_empty_otel_service_name_raises(self, tmp_path: Path):
        with pytest.raises(ConfigError, match="otelServiceName"):
            self._load(tmp_path, {"otelServiceName": "  "})

    def test_out_of_range_sample_ratio_raises(self, tmp_path: Path):
        with pytest.raises(ConfigError, match="otelSampleRatio"):
            self._load(tmp_path, {"otelSampleRatio": 1.5})

    def test_boolean_sample_ratio_rejected(self, tmp_path: Path):
        # bool is a subclass of int — must not be accepted as a ratio.
        with pytest.raises(ConfigError, match="otelSampleRatio"):
            self._load(tmp_path, {"otelSampleRatio": True})


# ---------------------------------------------------------------------------
# LlmsConfig — AI accessibility (per-page markdown + /llms.txt)
# ---------------------------------------------------------------------------


class TestLlmsConfigDefaults:
    def test_default_disabled(self):
        cfg = LlmsConfig()
        assert cfg.enabled is False
        assert cfg.auto_convert is False

    def test_default_in_pyxle_config(self):
        assert PyxleConfig().llms == LlmsConfig()


class TestLlmsConfigParsing:
    def _load(self, tmp_path: Path, llms_data) -> LlmsConfig:
        config_file = tmp_path / "pyxle.config.json"
        config_file.write_text(json.dumps({"llms": llms_data}))
        return load_config(tmp_path, config_path=config_file).llms

    def test_no_block_returns_defaults(self, tmp_path: Path):
        config_file = tmp_path / "pyxle.config.json"
        config_file.write_text(json.dumps({}))
        assert load_config(tmp_path, config_path=config_file).llms == LlmsConfig()

    def test_boolean_true_enables(self, tmp_path: Path):
        assert self._load(tmp_path, True).enabled is True

    def test_boolean_false_disables(self, tmp_path: Path):
        assert self._load(tmp_path, False).enabled is False

    def test_object_enables_by_default(self, tmp_path: Path):
        assert self._load(tmp_path, {"autoConvert": True}).enabled is True

    def test_object_explicit_disable(self, tmp_path: Path):
        cfg = self._load(tmp_path, {"enabled": False, "autoConvert": True})
        assert cfg.enabled is False
        assert cfg.auto_convert is True

    def test_auto_convert_flag(self, tmp_path: Path):
        assert self._load(tmp_path, {"enabled": True}).auto_convert is False
        assert self._load(tmp_path, {"autoConvert": True}).auto_convert is True

    def test_unknown_key_rejected(self, tmp_path: Path):
        with pytest.raises(ConfigError, match="llms"):
            self._load(tmp_path, {"handler": "app:conv"})

    def test_invalid_type_rejected(self, tmp_path: Path):
        with pytest.raises(ConfigError, match="llms"):
            self._load(tmp_path, ["not", "an", "object"])

    def test_invalid_enabled_type_rejected(self, tmp_path: Path):
        with pytest.raises(ConfigError, match="llms.enabled"):
            self._load(tmp_path, {"enabled": "yes"})

    def test_invalid_auto_convert_type_rejected(self, tmp_path: Path):
        with pytest.raises(ConfigError, match="llms.autoConvert"):
            self._load(tmp_path, {"autoConvert": "sure"})

    def test_carried_into_devserver_kwargs(self):
        kwargs = PyxleConfig(llms=LlmsConfig(enabled=True)).to_devserver_kwargs()
        assert kwargs["llms"] == LlmsConfig(enabled=True)
