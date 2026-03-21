"""
PWHL (Professional Women's Hockey League) Managers for Hockey Scoreboard Plugin

Uses HockeyTech/LeagueStat API instead of ESPN API.
Transforms HockeyTech data into ESPN-compatible format so the existing
Hockey base class rendering pipeline works without modification.
"""

import logging
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any, Dict, List, Optional

import pytz
import requests
from requests.adapters import HTTPAdapter
from urllib3.util.retry import Retry

from hockey import Hockey, HockeyLive
from sports import SportsRecent, SportsUpcoming

# HockeyTech API configuration for PWHL
PWHL_API_BASE_URL = "https://lscluster.hockeytech.com/feed/index.php"
PWHL_API_KEY = "446521baf8c38984"
PWHL_CLIENT_CODE = "pwhl"

# GameStatus codes from HockeyTech
HOCKEYTECH_STATUS_SCHEDULED = "1"
HOCKEYTECH_STATUS_IN_PROGRESS = "2"
HOCKEYTECH_STATUS_FINAL = "4"
HOCKEYTECH_STATUS_POSTPONED = "9"


def _transform_hockeytech_to_espn(game: Dict) -> Dict:
    """
    Transform a HockeyTech scorebar game object into ESPN-compatible event format.

    This adapter allows all existing SportsCore/Hockey parsing and rendering
    code to work unchanged with PWHL data.
    """
    game_status = str(game.get("GameStatus", "1"))

    # Map HockeyTech GameStatus to ESPN state
    if game_status == HOCKEYTECH_STATUS_FINAL:
        espn_state = "post"
        espn_name = "STATUS_FINAL"
    elif game_status == HOCKEYTECH_STATUS_IN_PROGRESS:
        espn_state = "in"
        espn_name = "STATUS_IN_PROGRESS"
    elif game_status == HOCKEYTECH_STATUS_POSTPONED:
        espn_state = "post"
        espn_name = "STATUS_POSTPONED"
    else:
        espn_state = "pre"
        espn_name = "STATUS_SCHEDULED"

    # Build status detail text
    status_string = game.get("GameStatusString", "")
    period_name = game.get("PeriodNameLong", "")
    game_clock = game.get("GameClock", "")
    if espn_state == "in" and period_name and game_clock:
        short_detail = f"{period_name} {game_clock}"
    elif espn_state == "post":
        short_detail = status_string or "Final"
    else:
        short_detail = game.get("ScheduledFormattedTime", "")

    # Build home team record string: W-L-OTL
    home_wins = game.get("HomeWins", "0")
    home_reg_losses = game.get("HomeRegulationLosses", "0")
    home_ot_losses = int(game.get("HomeOTLosses", "0")) + int(game.get("HomeShootoutLosses", "0"))
    home_record = f"{home_wins}-{home_reg_losses}-{home_ot_losses}"

    visitor_wins = game.get("VisitorWins", "0")
    visitor_reg_losses = game.get("VisitorRegulationLosses", "0")
    visitor_ot_losses = int(game.get("VisitorOTLosses", "0")) + int(game.get("VisitorShootoutLosses", "0"))
    visitor_record = f"{visitor_wins}-{visitor_reg_losses}-{visitor_ot_losses}"

    # Parse ISO8601 date for the ESPN format
    game_date_iso = game.get("GameDateISO8601", "")
    if not game_date_iso:
        # Fallback: construct from Date + ScheduledTime
        date_str = game.get("Date", "")
        time_str = game.get("ScheduledTime", "00:00:00")
        tz_str = game.get("Timezone", "America/New_York")
        if date_str:
            game_date_iso = f"{date_str}T{time_str}"

    # Period number
    period = 0
    try:
        period = int(game.get("Period", "0"))
    except (ValueError, TypeError):
        pass

    return {
        "id": game.get("ID", ""),
        "date": game_date_iso,
        "competitions": [
            {
                "competitors": [
                    {
                        "homeAway": "home",
                        "id": game.get("HomeID", ""),
                        "score": str(game.get("HomeGoals", "0")),
                        "team": {
                            "abbreviation": game.get("HomeCode", ""),
                            "displayName": game.get("HomeLongName", ""),
                            "name": game.get("HomeNickname", ""),
                            "logo": game.get("HomeLogo", ""),
                            "id": game.get("HomeID", ""),
                        },
                        "records": [{"summary": home_record}],
                        "statistics": [],
                    },
                    {
                        "homeAway": "away",
                        "id": game.get("VisitorID", ""),
                        "score": str(game.get("VisitorGoals", "0")),
                        "team": {
                            "abbreviation": game.get("VisitorCode", ""),
                            "displayName": game.get("VisitorLongName", ""),
                            "name": game.get("VisitorNickname", ""),
                            "logo": game.get("VisitorLogo", ""),
                            "id": game.get("VisitorID", ""),
                        },
                        "records": [{"summary": visitor_record}],
                        "statistics": [],
                    },
                ],
                "status": {
                    "type": {
                        "state": espn_state,
                        "name": espn_name,
                        "completed": game_status == HOCKEYTECH_STATUS_FINAL,
                        "shortDetail": short_detail,
                        "description": status_string,
                    },
                    "period": period,
                    "displayClock": game_clock,
                },
                "situation": None,
            }
        ],
    }


class BasePWHLManager(Hockey):
    """Base class for PWHL managers with HockeyTech data fetching."""

    _no_data_warning_logged = False
    _last_warning_time = 0
    _warning_cooldown = 60
    _shared_data = None
    _last_shared_update = 0
    _processed_games_cache = {}
    _processed_games_timestamp = 0

    def __init__(self, config: Dict[str, Any], display_manager, cache_manager):
        self.logger = logging.getLogger("PWHL")
        super().__init__(
            config=config,
            display_manager=display_manager,
            cache_manager=cache_manager,
            logger=self.logger,
            sport_key="pwhl",
        )

        # Check display modes to determine what data to fetch
        display_modes = self.mode_config.get("display_modes", {})
        self.recent_enabled = display_modes.get("hockey_recent", False)
        self.upcoming_enabled = display_modes.get("hockey_upcoming", False)
        self.live_enabled = display_modes.get("hockey_live", False)
        # PWHL doesn't use ESPN league identifier - we override fetch methods
        self.league = "pwhl"

        # Set up HockeyTech API session
        self._ht_session = requests.Session()
        retry_strategy = Retry(
            total=5,
            backoff_factor=1,
            status_forcelist=[429, 500, 502, 503, 504],
            allowed_methods=["GET", "HEAD", "OPTIONS"],
        )
        adapter = HTTPAdapter(max_retries=retry_strategy)
        self._ht_session.mount("https://", adapter)
        self._ht_session.mount("http://", adapter)

        self.logger.info(
            f"Initialized PWHL manager with display dimensions: "
            f"{self.display_width}x{self.display_height}"
        )
        self.logger.info(f"Logo directory: {self.logo_dir}")
        self.logger.info(
            f"Display modes - Recent: {self.recent_enabled}, "
            f"Upcoming: {self.upcoming_enabled}, Live: {self.live_enabled}"
        )
        self.logger.info(f"Favorite teams: {self.favorite_teams}")
        self.logger.info(f"Show favorite teams only: {self.show_favorite_teams_only}")
        self.logger.info(f"Show all live: {self.show_all_live}")

    def _fetch_hockeytech_scorebar(
        self, days_back: int = 7, days_ahead: int = 7, use_cache: bool = True
    ) -> Optional[Dict]:
        """
        Fetch PWHL games from HockeyTech scorebar endpoint and transform
        to ESPN-compatible format.
        """
        cache_key = f"pwhl_scorebar_{days_back}_{days_ahead}"

        if use_cache:
            cached_data = self.cache_manager.get(cache_key)
            if cached_data:
                if isinstance(cached_data, dict) and "events" in cached_data:
                    self.logger.info("Using cached PWHL scorebar data")
                    return cached_data

        try:
            params = {
                "feed": "modulekit",
                "view": "scorebar",
                "numberofdaysback": days_back,
                "numberofdaysahead": days_ahead,
                "key": PWHL_API_KEY,
                "client_code": PWHL_CLIENT_CODE,
                "fmt": "json",
            }
            response = self._ht_session.get(
                PWHL_API_BASE_URL,
                params=params,
                timeout=15,
            )
            response.raise_for_status()
            data = response.json()

            # HockeyTech wraps response in SiteKit.Scorebar
            scorebar = []
            if isinstance(data, dict):
                site_kit = data.get("SiteKit", data)
                scorebar = site_kit.get("Scorebar", [])
            elif isinstance(data, list):
                scorebar = data

            # Transform each game to ESPN format
            espn_events = []
            for game in scorebar:
                if isinstance(game, dict) and game.get("ID"):
                    espn_events.append(_transform_hockeytech_to_espn(game))

            self.logger.info(
                f"Fetched {len(espn_events)} PWHL games from HockeyTech "
                f"({days_back}d back, {days_ahead}d ahead)"
            )

            result = {"events": espn_events}

            # Cache the result
            cache_duration = self.mode_config.get(
                "season_cache_duration_seconds", 3600
            )
            self.cache_manager.set(cache_key, result, ttl=cache_duration)

            return result

        except requests.exceptions.RequestException as e:
            self.logger.error(f"HockeyTech API error for PWHL: {e}")
            return None
        except (ValueError, KeyError) as e:
            self.logger.error(f"Error parsing PWHL HockeyTech response: {e}")
            return None

    def _fetch_pwhl_season_data(self, use_cache: bool = True) -> Optional[Dict]:
        """Fetch full season data for recent/upcoming games."""
        now = datetime.now(pytz.utc)
        season_year = now.year
        if now.month < 8:
            season_year = now.year - 1
        cache_key = f"pwhl_season_{season_year}"

        if use_cache:
            cached_data = self.cache_manager.get(cache_key)
            if cached_data:
                if isinstance(cached_data, dict) and "events" in cached_data:
                    self.logger.info(f"Using cached PWHL season data for {season_year}")
                    return cached_data

        # Fetch broad range for season data
        # PWHL season roughly November to May
        return self._fetch_hockeytech_scorebar(
            days_back=180, days_ahead=90, use_cache=False
        )

    def _fetch_data(self) -> Optional[Dict]:
        """Fetch data - live manager fetches today, others fetch season."""
        if isinstance(self, PWHLLiveManager):
            return self._fetch_todays_pwhl_games()
        else:
            return self._fetch_pwhl_season_data(use_cache=True)

    def _fetch_todays_pwhl_games(self) -> Optional[Dict]:
        """Fetch only recent/today's games for live updates."""
        return self._fetch_hockeytech_scorebar(
            days_back=1, days_ahead=1, use_cache=False
        )

    def _fetch_todays_games(self) -> Optional[Dict]:
        """Override ESPN-based today's games fetch with HockeyTech version."""
        return self._fetch_todays_pwhl_games()

    def _get_weeks_data(self) -> Optional[Dict]:
        """Override ESPN-based weeks data fetch with HockeyTech version."""
        return self._fetch_hockeytech_scorebar(
            days_back=14, days_ahead=7, use_cache=True
        )


class PWHLLiveManager(BasePWHLManager, HockeyLive):
    """Manager for live PWHL games."""

    def __init__(self, config: Dict[str, Any], display_manager, cache_manager):
        super().__init__(config, display_manager, cache_manager)
        self.logger = logging.getLogger("PWHLLiveManager")

        if self.test_mode:
            self.current_game = {
                "id": "pwhl_test_001",
                "home_abbr": "BOS",
                "away_abbr": "TOR",
                "home_score": "3",
                "away_score": "2",
                "period": 2,
                "period_text": "P2",
                "home_id": "1",
                "away_id": "6",
                "clock": "12:34",
                "home_logo_path": Path(self.logo_dir, "BOS.png"),
                "away_logo_path": Path(self.logo_dir, "TOR.png"),
                "game_time": "7:00 PM",
                "game_date": "Mar 17",
                "is_live": True,
                "is_final": False,
                "is_upcoming": False,
            }
            self.live_games = [self.current_game]
            self.logger.info("Initialized PWHLLiveManager with test game: BOS vs TOR")
        else:
            self.logger.info("Initialized PWHLLiveManager in live mode")


class PWHLRecentManager(BasePWHLManager, SportsRecent):
    """Manager for recently completed PWHL games."""

    def __init__(self, config: Dict[str, Any], display_manager, cache_manager):
        super().__init__(config, display_manager, cache_manager)
        self.logger = logging.getLogger("PWHLRecentManager")
        self.logger.info(
            f"Initialized PWHLRecentManager with {len(self.favorite_teams)} favorite teams"
        )


class PWHLUpcomingManager(BasePWHLManager, SportsUpcoming):
    """Manager for upcoming PWHL games."""

    def __init__(self, config: Dict[str, Any], display_manager, cache_manager):
        super().__init__(config, display_manager, cache_manager)
        self.logger = logging.getLogger("PWHLUpcomingManager")
        self.logger.info(
            f"Initialized PWHLUpcomingManager with {len(self.favorite_teams)} favorite teams"
        )
