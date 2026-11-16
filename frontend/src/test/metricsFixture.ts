/**
 * TESTS ONLY. Verbatim GET /metrics body captured from an isolated MOCK-MODE backend (Redis DB 15, throwaway
 * prefix; no provider was called) after a handful of mock chats, including a fallback from mock-advanced.
 * It is the real exposition format (HELP/TYPE lines, lazily-created series, unlabelled counters, +Inf buckets).
 */
export const REAL_METRICS = `# HELP llm_router_build_info Build information (value is always 1).
# TYPE llm_router_build_info gauge
llm_router_build_info{mock_mode="true",version="0.1.0"} 1.0
# HELP llm_router_http_requests_total HTTP requests handled.
# TYPE llm_router_http_requests_total counter
llm_router_http_requests_total{method="GET",route="/health",status="200"} 1.0
llm_router_http_requests_total{method="GET",route="/metrics",status="401"} 1.0
llm_router_http_requests_total{method="GET",route="/api/v1/health/dependencies",status="200"} 1.0
llm_router_http_requests_total{method="GET",route="/health/dependencies",status="200"} 1.0
llm_router_http_requests_total{method="POST",route="/api/v1/chat",status="422"} 6.0
llm_router_http_requests_total{method="GET",route="/metrics",status="200"} 1.0
llm_router_http_requests_total{method="POST",route="/api/v1/chat",status="200"} 4.0
llm_router_http_requests_total{method="POST",route="/api/v1/chat",status="400"} 1.0
# HELP llm_router_http_request_duration_seconds HTTP request latency.
# TYPE llm_router_http_request_duration_seconds histogram
llm_router_http_request_duration_seconds_bucket{le="0.001",method="GET",route="/health"} 0.0
llm_router_http_request_duration_seconds_bucket{le="0.0025",method="GET",route="/health"} 0.0
llm_router_http_request_duration_seconds_bucket{le="0.005",method="GET",route="/health"} 0.0
llm_router_http_request_duration_seconds_bucket{le="0.01",method="GET",route="/health"} 0.0
llm_router_http_request_duration_seconds_bucket{le="0.025",method="GET",route="/health"} 0.0
llm_router_http_request_duration_seconds_bucket{le="0.05",method="GET",route="/health"} 0.0
llm_router_http_request_duration_seconds_bucket{le="0.1",method="GET",route="/health"} 1.0
llm_router_http_request_duration_seconds_bucket{le="0.25",method="GET",route="/health"} 1.0
llm_router_http_request_duration_seconds_bucket{le="0.5",method="GET",route="/health"} 1.0
llm_router_http_request_duration_seconds_bucket{le="1.0",method="GET",route="/health"} 1.0
llm_router_http_request_duration_seconds_bucket{le="2.5",method="GET",route="/health"} 1.0
llm_router_http_request_duration_seconds_bucket{le="5.0",method="GET",route="/health"} 1.0
llm_router_http_request_duration_seconds_bucket{le="10.0",method="GET",route="/health"} 1.0
llm_router_http_request_duration_seconds_bucket{le="30.0",method="GET",route="/health"} 1.0
llm_router_http_request_duration_seconds_bucket{le="60.0",method="GET",route="/health"} 1.0
llm_router_http_request_duration_seconds_bucket{le="+Inf",method="GET",route="/health"} 1.0
llm_router_http_request_duration_seconds_count{method="GET",route="/health"} 1.0
llm_router_http_request_duration_seconds_sum{method="GET",route="/health"} 0.08379439999771421
llm_router_http_request_duration_seconds_bucket{le="0.001",method="GET",route="/metrics"} 0.0
llm_router_http_request_duration_seconds_bucket{le="0.0025",method="GET",route="/metrics"} 0.0
llm_router_http_request_duration_seconds_bucket{le="0.005",method="GET",route="/metrics"} 1.0
llm_router_http_request_duration_seconds_bucket{le="0.01",method="GET",route="/metrics"} 2.0
llm_router_http_request_duration_seconds_bucket{le="0.025",method="GET",route="/metrics"} 2.0
llm_router_http_request_duration_seconds_bucket{le="0.05",method="GET",route="/metrics"} 2.0
llm_router_http_request_duration_seconds_bucket{le="0.1",method="GET",route="/metrics"} 2.0
llm_router_http_request_duration_seconds_bucket{le="0.25",method="GET",route="/metrics"} 2.0
llm_router_http_request_duration_seconds_bucket{le="0.5",method="GET",route="/metrics"} 2.0
llm_router_http_request_duration_seconds_bucket{le="1.0",method="GET",route="/metrics"} 2.0
llm_router_http_request_duration_seconds_bucket{le="2.5",method="GET",route="/metrics"} 2.0
llm_router_http_request_duration_seconds_bucket{le="5.0",method="GET",route="/metrics"} 2.0
llm_router_http_request_duration_seconds_bucket{le="10.0",method="GET",route="/metrics"} 2.0
llm_router_http_request_duration_seconds_bucket{le="30.0",method="GET",route="/metrics"} 2.0
llm_router_http_request_duration_seconds_bucket{le="60.0",method="GET",route="/metrics"} 2.0
llm_router_http_request_duration_seconds_bucket{le="+Inf",method="GET",route="/metrics"} 2.0
llm_router_http_request_duration_seconds_count{method="GET",route="/metrics"} 2.0
llm_router_http_request_duration_seconds_sum{method="GET",route="/metrics"} 0.009823400003369898
llm_router_http_request_duration_seconds_bucket{le="0.001",method="GET",route="/api/v1/health/dependencies"} 0.0
llm_router_http_request_duration_seconds_bucket{le="0.0025",method="GET",route="/api/v1/health/dependencies"} 0.0
llm_router_http_request_duration_seconds_bucket{le="0.005",method="GET",route="/api/v1/health/dependencies"} 0.0
llm_router_http_request_duration_seconds_bucket{le="0.01",method="GET",route="/api/v1/health/dependencies"} 0.0
llm_router_http_request_duration_seconds_bucket{le="0.025",method="GET",route="/api/v1/health/dependencies"} 0.0
llm_router_http_request_duration_seconds_bucket{le="0.05",method="GET",route="/api/v1/health/dependencies"} 0.0
llm_router_http_request_duration_seconds_bucket{le="0.1",method="GET",route="/api/v1/health/dependencies"} 0.0
llm_router_http_request_duration_seconds_bucket{le="0.25",method="GET",route="/api/v1/health/dependencies"} 0.0
llm_router_http_request_duration_seconds_bucket{le="0.5",method="GET",route="/api/v1/health/dependencies"} 0.0
llm_router_http_request_duration_seconds_bucket{le="1.0",method="GET",route="/api/v1/health/dependencies"} 0.0
llm_router_http_request_duration_seconds_bucket{le="2.5",method="GET",route="/api/v1/health/dependencies"} 0.0
llm_router_http_request_duration_seconds_bucket{le="5.0",method="GET",route="/api/v1/health/dependencies"} 1.0
llm_router_http_request_duration_seconds_bucket{le="10.0",method="GET",route="/api/v1/health/dependencies"} 1.0
llm_router_http_request_duration_seconds_bucket{le="30.0",method="GET",route="/api/v1/health/dependencies"} 1.0
llm_router_http_request_duration_seconds_bucket{le="60.0",method="GET",route="/api/v1/health/dependencies"} 1.0
llm_router_http_request_duration_seconds_bucket{le="+Inf",method="GET",route="/api/v1/health/dependencies"} 1.0
llm_router_http_request_duration_seconds_count{method="GET",route="/api/v1/health/dependencies"} 1.0
llm_router_http_request_duration_seconds_sum{method="GET",route="/api/v1/health/dependencies"} 3.018900399998529
llm_router_http_request_duration_seconds_bucket{le="0.001",method="GET",route="/health/dependencies"} 0.0
llm_router_http_request_duration_seconds_bucket{le="0.0025",method="GET",route="/health/dependencies"} 0.0
llm_router_http_request_duration_seconds_bucket{le="0.005",method="GET",route="/health/dependencies"} 0.0
llm_router_http_request_duration_seconds_bucket{le="0.01",method="GET",route="/health/dependencies"} 0.0
llm_router_http_request_duration_seconds_bucket{le="0.025",method="GET",route="/health/dependencies"} 0.0
llm_router_http_request_duration_seconds_bucket{le="0.05",method="GET",route="/health/dependencies"} 0.0
llm_router_http_request_duration_seconds_bucket{le="0.1",method="GET",route="/health/dependencies"} 0.0
llm_router_http_request_duration_seconds_bucket{le="0.25",method="GET",route="/health/dependencies"} 0.0
llm_router_http_request_duration_seconds_bucket{le="0.5",method="GET",route="/health/dependencies"} 0.0
llm_router_http_request_duration_seconds_bucket{le="1.0",method="GET",route="/health/dependencies"} 0.0
llm_router_http_request_duration_seconds_bucket{le="2.5",method="GET",route="/health/dependencies"} 0.0
llm_router_http_request_duration_seconds_bucket{le="5.0",method="GET",route="/health/dependencies"} 1.0
llm_router_http_request_duration_seconds_bucket{le="10.0",method="GET",route="/health/dependencies"} 1.0
llm_router_http_request_duration_seconds_bucket{le="30.0",method="GET",route="/health/dependencies"} 1.0
llm_router_http_request_duration_seconds_bucket{le="60.0",method="GET",route="/health/dependencies"} 1.0
llm_router_http_request_duration_seconds_bucket{le="+Inf",method="GET",route="/health/dependencies"} 1.0
llm_router_http_request_duration_seconds_count{method="GET",route="/health/dependencies"} 1.0
llm_router_http_request_duration_seconds_sum{method="GET",route="/health/dependencies"} 3.01103349999903
llm_router_http_request_duration_seconds_bucket{le="0.001",method="POST",route="/api/v1/chat"} 0.0
llm_router_http_request_duration_seconds_bucket{le="0.0025",method="POST",route="/api/v1/chat"} 0.0
llm_router_http_request_duration_seconds_bucket{le="0.005",method="POST",route="/api/v1/chat"} 3.0
llm_router_http_request_duration_seconds_bucket{le="0.01",method="POST",route="/api/v1/chat"} 4.0
llm_router_http_request_duration_seconds_bucket{le="0.025",method="POST",route="/api/v1/chat"} 8.0
llm_router_http_request_duration_seconds_bucket{le="0.05",method="POST",route="/api/v1/chat"} 8.0
llm_router_http_request_duration_seconds_bucket{le="0.1",method="POST",route="/api/v1/chat"} 10.0
llm_router_http_request_duration_seconds_bucket{le="0.25",method="POST",route="/api/v1/chat"} 10.0
llm_router_http_request_duration_seconds_bucket{le="0.5",method="POST",route="/api/v1/chat"} 11.0
llm_router_http_request_duration_seconds_bucket{le="1.0",method="POST",route="/api/v1/chat"} 11.0
llm_router_http_request_duration_seconds_bucket{le="2.5",method="POST",route="/api/v1/chat"} 11.0
llm_router_http_request_duration_seconds_bucket{le="5.0",method="POST",route="/api/v1/chat"} 11.0
llm_router_http_request_duration_seconds_bucket{le="10.0",method="POST",route="/api/v1/chat"} 11.0
llm_router_http_request_duration_seconds_bucket{le="30.0",method="POST",route="/api/v1/chat"} 11.0
llm_router_http_request_duration_seconds_bucket{le="60.0",method="POST",route="/api/v1/chat"} 11.0
llm_router_http_request_duration_seconds_bucket{le="+Inf",method="POST",route="/api/v1/chat"} 11.0
llm_router_http_request_duration_seconds_count{method="POST",route="/api/v1/chat"} 11.0
llm_router_http_request_duration_seconds_sum{method="POST",route="/api/v1/chat"} 0.5835397000046214
# HELP llm_router_http_requests_in_progress HTTP requests in flight.
# TYPE llm_router_http_requests_in_progress gauge
llm_router_http_requests_in_progress 1.0
# HELP llm_router_errors_total Failed HTTP requests by error category and code.
# TYPE llm_router_errors_total counter
llm_router_errors_total{category="auth",code="unauthorized"} 1.0
llm_router_errors_total{category="validation",code="validation_error"} 6.0
llm_router_errors_total{category="client",code="unknown_model"} 1.0
# HELP llm_router_chat_requests_total Chat requests by classification, routing and cache outcome.
# TYPE llm_router_chat_requests_total counter
llm_router_chat_requests_total{cache_status="miss",complexity="SIMPLE",model_used="mock-fast",outcome="success",selected_model="mock-fast"} 2.0
llm_router_chat_requests_total{cache_status="hit_exact",complexity="SIMPLE",model_used="mock-fast",outcome="success",selected_model="mock-fast"} 1.0
llm_router_chat_requests_total{cache_status="miss",complexity="COMPLEX",model_used="mock-balanced",outcome="success",selected_model="mock-advanced"} 1.0
# HELP llm_router_provider_attempts_total Provider attempts (incl. skipped candidates).
# TYPE llm_router_provider_attempts_total counter
llm_router_provider_attempts_total{error_kind="none",model="mock-fast",outcome="success",provider="mock"} 2.0
llm_router_provider_attempts_total{error_kind="unavailable",model="mock-advanced",outcome="error",provider="mock"} 2.0
llm_router_provider_attempts_total{error_kind="none",model="mock-balanced",outcome="success",provider="mock"} 1.0
# HELP llm_router_provider_latency_seconds Latency of provider invocations (success or failure).
# TYPE llm_router_provider_latency_seconds histogram
llm_router_provider_latency_seconds_bucket{le="0.005",model="mock-fast",outcome="success",provider="mock"} 0.0
llm_router_provider_latency_seconds_bucket{le="0.01",model="mock-fast",outcome="success",provider="mock"} 0.0
llm_router_provider_latency_seconds_bucket{le="0.025",model="mock-fast",outcome="success",provider="mock"} 0.0
llm_router_provider_latency_seconds_bucket{le="0.05",model="mock-fast",outcome="success",provider="mock"} 2.0
llm_router_provider_latency_seconds_bucket{le="0.1",model="mock-fast",outcome="success",provider="mock"} 2.0
llm_router_provider_latency_seconds_bucket{le="0.25",model="mock-fast",outcome="success",provider="mock"} 2.0
llm_router_provider_latency_seconds_bucket{le="0.5",model="mock-fast",outcome="success",provider="mock"} 2.0
llm_router_provider_latency_seconds_bucket{le="1.0",model="mock-fast",outcome="success",provider="mock"} 2.0
llm_router_provider_latency_seconds_bucket{le="2.0",model="mock-fast",outcome="success",provider="mock"} 2.0
llm_router_provider_latency_seconds_bucket{le="5.0",model="mock-fast",outcome="success",provider="mock"} 2.0
llm_router_provider_latency_seconds_bucket{le="10.0",model="mock-fast",outcome="success",provider="mock"} 2.0
llm_router_provider_latency_seconds_bucket{le="20.0",model="mock-fast",outcome="success",provider="mock"} 2.0
llm_router_provider_latency_seconds_bucket{le="30.0",model="mock-fast",outcome="success",provider="mock"} 2.0
llm_router_provider_latency_seconds_bucket{le="60.0",model="mock-fast",outcome="success",provider="mock"} 2.0
llm_router_provider_latency_seconds_bucket{le="120.0",model="mock-fast",outcome="success",provider="mock"} 2.0
llm_router_provider_latency_seconds_bucket{le="+Inf",model="mock-fast",outcome="success",provider="mock"} 2.0
llm_router_provider_latency_seconds_count{model="mock-fast",outcome="success",provider="mock"} 2.0
llm_router_provider_latency_seconds_sum{model="mock-fast",outcome="success",provider="mock"} 0.054695800001354655
llm_router_provider_latency_seconds_bucket{le="0.005",model="mock-advanced",outcome="error",provider="mock"} 0.0
llm_router_provider_latency_seconds_bucket{le="0.01",model="mock-advanced",outcome="error",provider="mock"} 0.0
llm_router_provider_latency_seconds_bucket{le="0.025",model="mock-advanced",outcome="error",provider="mock"} 1.0
llm_router_provider_latency_seconds_bucket{le="0.05",model="mock-advanced",outcome="error",provider="mock"} 2.0
llm_router_provider_latency_seconds_bucket{le="0.1",model="mock-advanced",outcome="error",provider="mock"} 2.0
llm_router_provider_latency_seconds_bucket{le="0.25",model="mock-advanced",outcome="error",provider="mock"} 2.0
llm_router_provider_latency_seconds_bucket{le="0.5",model="mock-advanced",outcome="error",provider="mock"} 2.0
llm_router_provider_latency_seconds_bucket{le="1.0",model="mock-advanced",outcome="error",provider="mock"} 2.0
llm_router_provider_latency_seconds_bucket{le="2.0",model="mock-advanced",outcome="error",provider="mock"} 2.0
llm_router_provider_latency_seconds_bucket{le="5.0",model="mock-advanced",outcome="error",provider="mock"} 2.0
llm_router_provider_latency_seconds_bucket{le="10.0",model="mock-advanced",outcome="error",provider="mock"} 2.0
llm_router_provider_latency_seconds_bucket{le="20.0",model="mock-advanced",outcome="error",provider="mock"} 2.0
llm_router_provider_latency_seconds_bucket{le="30.0",model="mock-advanced",outcome="error",provider="mock"} 2.0
llm_router_provider_latency_seconds_bucket{le="60.0",model="mock-advanced",outcome="error",provider="mock"} 2.0
llm_router_provider_latency_seconds_bucket{le="120.0",model="mock-advanced",outcome="error",provider="mock"} 2.0
llm_router_provider_latency_seconds_bucket{le="+Inf",model="mock-advanced",outcome="error",provider="mock"} 2.0
llm_router_provider_latency_seconds_count{model="mock-advanced",outcome="error",provider="mock"} 2.0
llm_router_provider_latency_seconds_sum{model="mock-advanced",outcome="error",provider="mock"} 0.05766119999680086
llm_router_provider_latency_seconds_bucket{le="0.005",model="mock-balanced",outcome="success",provider="mock"} 0.0
llm_router_provider_latency_seconds_bucket{le="0.01",model="mock-balanced",outcome="success",provider="mock"} 0.0
llm_router_provider_latency_seconds_bucket{le="0.025",model="mock-balanced",outcome="success",provider="mock"} 1.0
llm_router_provider_latency_seconds_bucket{le="0.05",model="mock-balanced",outcome="success",provider="mock"} 1.0
llm_router_provider_latency_seconds_bucket{le="0.1",model="mock-balanced",outcome="success",provider="mock"} 1.0
llm_router_provider_latency_seconds_bucket{le="0.25",model="mock-balanced",outcome="success",provider="mock"} 1.0
llm_router_provider_latency_seconds_bucket{le="0.5",model="mock-balanced",outcome="success",provider="mock"} 1.0
llm_router_provider_latency_seconds_bucket{le="1.0",model="mock-balanced",outcome="success",provider="mock"} 1.0
llm_router_provider_latency_seconds_bucket{le="2.0",model="mock-balanced",outcome="success",provider="mock"} 1.0
llm_router_provider_latency_seconds_bucket{le="5.0",model="mock-balanced",outcome="success",provider="mock"} 1.0
llm_router_provider_latency_seconds_bucket{le="10.0",model="mock-balanced",outcome="success",provider="mock"} 1.0
llm_router_provider_latency_seconds_bucket{le="20.0",model="mock-balanced",outcome="success",provider="mock"} 1.0
llm_router_provider_latency_seconds_bucket{le="30.0",model="mock-balanced",outcome="success",provider="mock"} 1.0
llm_router_provider_latency_seconds_bucket{le="60.0",model="mock-balanced",outcome="success",provider="mock"} 1.0
llm_router_provider_latency_seconds_bucket{le="120.0",model="mock-balanced",outcome="success",provider="mock"} 1.0
llm_router_provider_latency_seconds_bucket{le="+Inf",model="mock-balanced",outcome="success",provider="mock"} 1.0
llm_router_provider_latency_seconds_count{model="mock-balanced",outcome="success",provider="mock"} 1.0
llm_router_provider_latency_seconds_sum{model="mock-balanced",outcome="success",provider="mock"} 0.023697999997239094
# HELP llm_router_retries_total Retries of the same model after a transient error.
# TYPE llm_router_retries_total counter
llm_router_retries_total{model="mock-advanced"} 1.0
# HELP llm_router_fallbacks_total Fallback attempts from the primary to another model.
# TYPE llm_router_fallbacks_total counter
llm_router_fallbacks_total{from_model="mock-advanced",to_model="mock-balanced"} 1.0
# HELP llm_router_cache_lookups_total Response-cache lookups by result.
# TYPE llm_router_cache_lookups_total counter
llm_router_cache_lookups_total{status="miss"} 3.0
llm_router_cache_lookups_total{status="hit_exact"} 1.0
# HELP llm_router_cache_hit_ratio hits / (hits + misses) for this process since start (prefer rate() on cache_lookups_total).
# TYPE llm_router_cache_hit_ratio gauge
llm_router_cache_hit_ratio 0.25
# HELP llm_router_tokens_total Tokens consumed by provider calls (cache hits excluded).
# TYPE llm_router_tokens_total counter
llm_router_tokens_total{direction="input",model="mock-fast",source="estimated"} 6.0
llm_router_tokens_total{direction="output",model="mock-fast",source="estimated"} 100.0
llm_router_tokens_total{direction="input",model="mock-balanced",source="estimated"} 47.0
llm_router_tokens_total{direction="output",model="mock-balanced",source="estimated"} 51.0
# HELP llm_router_estimated_cost_usd_total Estimated cost in USD from registry pricing (pricing=placeholder in mock mode).
# TYPE llm_router_estimated_cost_usd_total counter
llm_router_estimated_cost_usd_total{model="mock-fast",pricing="placeholder"} 4.06e-05
llm_router_estimated_cost_usd_total{model="mock-balanced",pricing="placeholder"} 0.0002
# HELP llm_router_rate_limit_rejections_total Requests or model calls rejected by rate limits.
# TYPE llm_router_rate_limit_rejections_total counter
# HELP llm_router_budget_rejections_total Real provider attempts refused by spend safeguards.
# TYPE llm_router_budget_rejections_total counter
# HELP llm_router_embedding_budget_events_total Budget outcomes of paid embedding calls (reserved/refused/settled/released/kept_*).
# TYPE llm_router_embedding_budget_events_total counter
# HELP llm_router_embedding_estimated_cost_usd_total Estimated USD of paid embedding calls with provider-reported usage.
# TYPE llm_router_embedding_estimated_cost_usd_total counter
llm_router_embedding_estimated_cost_usd_total 0.0
`
