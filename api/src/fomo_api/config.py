from __future__ import annotations

from pydantic import Field
from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(
        env_prefix="FOMO_API_",
        env_file=".env",
        extra="ignore",
    )

    redis_url: str = Field(default="redis://localhost:6379/0")
    # Data API base (CONFIRMED via static recon of fomoFetch-BjKROXCg.js:
    # `function pt(){return"https://prod-api.fomo.family"}`). All read calls go here.
    upstream_base: str = Field(default="https://prod-api.fomo.family")
    # SPA base (https://fomo.family) used ONLY for the Privy OAuth login step.
    # The `/token` route handles the privy_oauth_code/state/provider callback.
    upstream_app_base: str = Field(default="https://fomo.family")
    # Value of the X-Supported-Chains request header (CONFIRMED: comma-joined
    # chain ids from chains-B91MAH-X.js `ne()` = `f().join(",")`). Ethereum (1)
    # is gated behind fomo's eth_mainnet feature flag, so it is excluded by
    # default. Override FOMO_API_UPSTREAM_SUPPORTED_CHAINS to add it.
    upstream_supported_chains: str = Field(default="56,143,4663,8453,1399811149")
    # Upstream endpoint paths. ALL CONFIRMED via live network capture of an
    # authenticated fomo.family session (2026-07-25, see captured_endpoints.json).
    # Every response is wrapped in {success, message, responseObject, statusCode}.
    # The client raises UPSTREAM_CHANGED on shape mismatch rather than fabricating
    # (FR-007). Override any path via its env var if fomo.family changes routes.
    upstream_leaderboard_path: str = Field(default="/v2/leaderboard")  # CONFIRMED
    # Per-period leaderboard variants (responseObject.leaderboard[] with the
    # matching pnl<period> field). "all" -> upstream_leaderboard_path (totalPnL).
    upstream_leaderboard_period_paths: dict[str, str] = Field(
        default_factory=lambda: {
            "24h": "/v2/leaderboard/24h",
            "7d": "/v2/leaderboard/7d",
            "30d": "/v2/leaderboard/30d",
        }
    )
    upstream_trader_path: str = Field(default="/v2/users/{trader_id}")  # CONFIRMED
    upstream_trader_by_handle_path: str = Field(
        default="/v2/users/userHandle/{handle}"
    )  # CONFIRMED
    upstream_activity_path: str = Field(default="/v2/users/{trader_id}/swaps")  # CONFIRMED
    upstream_trades_path: str = Field(default="/trades")  # CONFIRMED (?userId=)
    upstream_balances_path: str = Field(default="/v2/users/{trader_id}/balances")  # CONFIRMED
    upstream_pnl_snapshot_path: str = Field(
        default="/v2/userTokens/aggregatedSnapshot"
    )  # CONFIRMED (?userId=&interval=)
    # Alerts are surfaced via the global trading-activity feed (no per-trader
    # alert endpoint exists); filter by userId client-side.
    upstream_alerts_path: str = Field(default="/feed/tradingActivity")  # CONFIRMED
    # Other CONFIRMED endpoints from live network capture (2026-07-25)
    upstream_feed_token_thesis_path: str = Field(default="/feed/token/thesis")  # CONFIRMED
    upstream_filter_tokens_path: str = Field(default="/proxy/filterTokens")  # CONFIRMED
    upstream_hodlers_top_path: str = Field(default="/hodlers/top")  # CONFIRMED
    # Trade comments and trader spotlight (CONFIRMED via live capture 2026-07-25).
    upstream_trade_comments_path: str = Field(default="/trades/{trade_id}/comments")  # CONFIRMED
    upstream_spotlight_path: str = Field(default="/v2/users/{trader_id}/spotlight")  # CONFIRMED
    # Token market-data endpoints (CONFIRMED via live probe 2026-07-25; these are
    # POST /proxy/* routes whose request bodies were verified against the live API).
    upstream_get_bars_path: str = Field(default="/proxy/getBarsNew")  # POST {symbol,resolution,from,to,countBack}
    upstream_token_details_path: str = Field(default="/proxy/tokenDetails")  # POST {tokenId}
    upstream_token_warnings_path: str = Field(default="/proxy/tokenWarnings")  # POST {address,networkId}
    upstream_trending_tokens_path: str = Field(default="/proxy/trendingTokens")  # POST {}
    upstream_most_held_path: str = Field(default="/proxy/mostHeld")  # POST {}
    upstream_verified_tokens_path: str = Field(default="/proxy/verifiedTokens")  # GET
    upstream_hodlers_friends_path: str = Field(default="/hodlers/friends")  # POST {tokens:[...]}
    upstream_feed_path: str = Field(default="/feed")  # GET (global social feed)
    upstream_feed_token_path: str = Field(default="/feed/token")  # GET (per-token feed)
    # GET /feed requires a non-empty feedTypes[] array. CONFIRMED enum members
    # (2026-07-25 live probe): multi_user_buy, multi_user_sell, large_buy. Unknown
    # values are dropped upstream; an all-unknown list 400s, so this is the default.
    feed_default_types: list[str] = Field(
        default_factory=lambda: ["multi_user_buy", "multi_user_sell", "large_buy"]
    )
    session_ttl_seconds: int = Field(default=900)
    rate_limit_per_minute: int = Field(default=60)
    alert_poll_interval_seconds: int = Field(default=10)
    default_page_size: int = Field(default=20)
    max_page_size: int = Field(default=100)
    upstream_request_timeout_seconds: float = Field(default=15.0)
    dev: bool = Field(default=False)
    # When True, upstream requests go through curl_cffi impersonating Chrome to
    # bypass Cloudflare's TLS/JA3 bot detection (plain httpx gets a 403 HTML
    # challenge page). Set FOMO_API_UPSTREAM_IMPERSONATE=false to force httpx —
    # the test suite relies on this so respx can intercept requests.
    upstream_impersonate: bool = Field(default=True)
    # Live Privy login opt-in. When False (default), PrivyLoginService.login()
    # raises PrivyLoginError instead of launching a real browser — so the test
    # suite and default deployments never open Chrome. Set
    # FOMO_API_ENABLE_LIVE_LOGIN=true to drive an interactive login.
    enable_live_login: bool = Field(default=False)
    # --- Unattended auto-extraction ---
    # File where the rotating Privy secrets (refresh_token + pat, which change on
    # every renewal) are persisted so the background refresher keeps the access
    # token fresh across restarts with NO manual login. Holds live credentials —
    # written 0600, never logged (FR-013). Delete it to revoke.
    credential_state_file: str = Field(default=".privy_state.json")
    # One-time browser-capture dump used to seed the state file on first run only.
    # After the first successful bootstrap the state file is the source of truth.
    credential_bootstrap_dump: str = Field(default="privy_storage_dump.json")
    # Master switch for the startup bootstrap (seed creds + refresh + stable key).
    auto_bootstrap: bool = Field(default=True)
    # fomo.family's Privy client id — a STABLE per-app identifier sent as the
    # `privy-client-id` header. CONFIRMED REQUIRED for POST auth.privy.io/v1/sessions
    # to return 200 (without it Privy answers 400 "Invalid auth token"). It is
    # not reliably present in localStorage as `client-…`, so we default to the
    # confirmed value and let a captured client_id override it when available.
    privy_client_id: str = Field(default="client-WY5gFSayQjxnQhG4rP6SnwPAyPZWZpNRhJ6b9rzMnYwqH")


settings = Settings()
