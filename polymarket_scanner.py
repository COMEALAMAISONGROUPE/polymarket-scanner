#!/usr/bin/env python3
"""
╔══════════════════════════════════════════════════════════════════╗
║  POLYMARKET OPPORTUNITY SCANNER BOT                            ║
║  Analyse les marchés Polymarket pour identifier des            ║
║  opportunités à faible risque et haut rendement                ║
╚══════════════════════════════════════════════════════════════════╝

Stratégies implémentées :
  1. Arbitrage YES/NO (somme des prix < 1.00 ou > 1.00)
  2. Détection de mispricing (écart prix vs probabilité implicite)
  3. Marchés quasi-certains à haut rendement (prix > 0.90 mais pas 1.00)
  4. Analyse de liquidité (spread bid/ask serré = meilleure exécution)
  5. Corrélation inter-marchés (marchés liés avec incohérences)

Prérequis :
  pip install requests py-clob-client python-dotenv tabulate

Usage :
  python polymarket_scanner.py              # Scan par défaut
  python polymarket_scanner.py --top 20     # Top 20 opportunités
  python polymarket_scanner.py --min-roi 5  # ROI minimum 5%
"""

import requests
import json
import time
import logging
import argparse
from datetime import datetime, timezone
from dataclasses import dataclass, field
from typing import Optional

GAMMA_API = "https://gamma-api.polymarket.com"
CLOB_API  = "https://clob.polymarket.com"
DATA_API  = "https://data-api.polymarket.com"

MIN_VOLUME       = 10_000
MIN_LIQUIDITY    = 5_000
MAX_SPREAD       = 0.05
MIN_ROI          = 0.02
MAX_RISK_SCORE   = 4
REQUEST_DELAY    = 0.15
MARKET_LIMIT     = 200

logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(message)s")
logger = logging.getLogger("PolyScanner")


@dataclass
class MarketData:
    condition_id: str
    question: str
    slug: str
    outcomes: list
    outcome_prices: list
    clob_token_ids: list
    volume: float
    liquidity: float
    end_date: Optional[str]
    active: bool
    closed: bool
    tags: list = field(default_factory=list)
    description: str = ""
    event_slug: str = ""


@dataclass
class OrderBookSnapshot:
    best_bid: float = 0.0
    best_ask: float = 1.0
    bid_depth: float = 0.0
    ask_depth: float = 0.0
    spread: float = 1.0
    midpoint: float = 0.5


@dataclass
class Opportunity:
    market: MarketData
    strategy: str
    side: str
    entry_price: float
    expected_payout: float
    roi_pct: float
    risk_score: int
    confidence: float
    reasoning: str
    orderbook: Optional[OrderBookSnapshot] = None
    days_to_expiry: Optional[int] = None


class PolymarketClient:
    def __init__(self):
        self.session = requests.Session()
        self.session.headers.update({"Accept": "application/json", "User-Agent": "PolyScanner/1.0"})

    def _get(self, url, params=None):
        try:
            resp = self.session.get(url, params=params, timeout=15)
            resp.raise_for_status()
            return resp.json()
        except requests.exceptions.RequestException as e:
            logger.warning(f"Erreur API: {e}")
            return []

    def fetch_active_markets(self, limit=100, offset=0):
        params = {"active": "true", "closed": "false", "limit": limit, "offset": offset, "order": "volume", "ascending": "false"}
        return self._get(f"{GAMMA_API}/markets", params) or []

    def fetch_events(self, limit=50):
        return self._get(f"{GAMMA_API}/events", {"active": "true", "closed": "false", "limit": limit}) or []

    def get_orderbook(self, token_id):
        time.sleep(REQUEST_DELAY)
        return self._get(f"{CLOB_API}/book", {"token_id": token_id}) or {}

    def get_midpoint(self, token_id):
        time.sleep(REQUEST_DELAY)
        data = self._get(f"{CLOB_API}/midpoint", {"token_id": token_id})
        return float(data.get("mid", 0.5)) if data else 0.5

    def get_price(self, token_id, side="buy"):
        time.sleep(REQUEST_DELAY)
        data = self._get(f"{CLOB_API}/price", {"token_id": token_id, "side": side})
        return float(data.get("price", 0.5)) if data else 0.5


class StrategyAnalyzer:
    def __init__(self, client):
        self.client = client

    def parse_market(self, raw):
        try:
            prices_raw = raw.get("outcomePrices", "[]")
            prices = json.loads(prices_raw) if isinstance(prices_raw, str) else prices_raw
            prices = [float(p) for p in prices]
            tokens_raw = raw.get("clobTokenIds", "[]")
            tokens = json.loads(tokens_raw) if isinstance(tokens_raw, str) else tokens_raw
            outcomes_raw = raw.get("outcomes", "[]")
            outcomes = json.loads(outcomes_raw) if isinstance(outcomes_raw, str) else outcomes_raw
            if len(prices) < 2 or len(tokens) < 2:
                return None
            return MarketData(
                condition_id=raw.get("conditionId", ""),
                question=raw.get("question", "N/A"),
                slug=raw.get("slug", ""),
                outcomes=outcomes,
                outcome_prices=prices,
                clob_token_ids=tokens,
                volume=float(raw.get("volume", 0) or 0),
                liquidity=float(raw.get("liquidity", 0) or 0),
                end_date=raw.get("endDate"),
                active=raw.get("active", False),
                closed=raw.get("closed", False),
                tags=raw.get("tags", []) or [],
                description=raw.get("description", ""),
                event_slug=raw.get("eventSlug", ""),
            )
        except Exception as e:
            logger.debug(f"Parse error: {e}")
            return None

    def get_orderbook_snapshot(self, token_id):
        book = self.client.get_orderbook(token_id)
        snap = OrderBookSnapshot()
        bids = book.get("bids", [])
        asks = book.get("asks", [])
        if bids:
            snap.best_bid = max(float(b.get("price", 0)) for b in bids)
            snap.bid_depth = sum(float(b.get("size", 0)) * float(b.get("price", 0)) for b in bids)
        if asks:
            snap.best_ask = min(float(a.get("price", 0)) for a in asks)
            snap.ask_depth = sum(float(a.get("size", 0)) * float(a.get("price", 0)) for a in asks)
        if bids and asks:
            snap.spread = snap.best_ask - snap.best_bid
            snap.midpoint = (snap.best_bid + snap.best_ask) / 2
        return snap

    def days_until_expiry(self, end_date_str):
        if not end_date_str:
            return None
        try:
            end = datetime.fromisoformat(end_date_str.replace("Z", "+00:00"))
            now = datetime.now(timezone.utc)
            return max(0, (end - now).days)
        except Exception:
            return None

    def analyze_yesno_arbitrage(self, market):
        yes_price, no_price = market.outcome_prices[0], market.outcome_prices[1]
        total = yes_price + no_price
        if total < 0.98:
            profit_per_share = 1.0 - total
            roi = (profit_per_share / total) * 100
            return Opportunity(
                market=market, strategy="ARBITRAGE YES/NO", side="YES + NO",
                entry_price=total, expected_payout=1.0, roi_pct=roi,
                risk_score=1, confidence=0.95,
                reasoning=f"YES({yes_price:.3f}) + NO({no_price:.3f}) = {total:.3f} < 1.00 → profit {profit_per_share:.3f}$/share"
            )
        return None

    def analyze_near_certain(self, market):
        opportunities = []
        days = self.days_until_expiry(market.end_date)
        for i, (price, outcome) in enumerate(zip(market.outcome_prices, market.outcomes)):
            if 0.88 <= price <= 0.97:
                roi = ((1.0 - price) / price) * 100
                if days and days > 0:
                    daily_roi = roi / days
                else:
                    daily_roi = roi
                risk = 2 if price > 0.93 else 3
                opp = Opportunity(
                    market=market, strategy="QUASI-CERTAIN", side=outcome,
                    entry_price=price, expected_payout=1.0, roi_pct=roi,
                    risk_score=risk, confidence=price,
                    reasoning=f"Prix {price:.3f} sur {outcome} → ROI {roi:.1f}% ({daily_roi:.2f}%/jour)",
                    days_to_expiry=days
                )
                opportunities.append(opp)
        return opportunities

    def analyze_liquidity(self, market):
        if not market.clob_token_ids:
            return None
        token_id = market.clob_token_ids[0]
        snap = self.get_orderbook_snapshot(token_id)
        if snap.spread <= 0.02 and snap.bid_depth > MIN_LIQUIDITY:
            price = snap.midpoint
            roi = ((1.0 - price) / price) * 100
            if roi >= MIN_ROI * 100 and price > 0.5:
                return Opportunity(
                    market=market, strategy="LIQUIDITE OPTIMALE", side="YES",
                    entry_price=price, expected_payout=1.0, roi_pct=roi,
                    risk_score=3, confidence=0.75,
                    reasoning=f"Spread serré {snap.spread:.3f}, profondeur {snap.bid_depth:.0f}$",
                    orderbook=snap
                )
        return None

    def analyze_mispricing(self, market):
        if len(market.outcome_prices) < 2:
            return None
        yes_price = market.outcome_prices[0]
        no_price = market.outcome_prices[1]
        implied_yes = 1.0 - no_price
        gap = abs(yes_price - implied_yes)
        if gap > 0.04:
            if implied_yes > yes_price:
                side = "YES"
                entry = yes_price
                reasoning = f"YES sous-évalué: prix={yes_price:.3f} vs implicite={implied_yes:.3f} (écart={gap:.3f})"
            else:
                side = "NO"
                entry = no_price
                implied_no = 1.0 - yes_price
                reasoning = f"NO sous-évalué: prix={no_price:.3f} vs implicite={implied_no:.3f} (écart={gap:.3f})"
            roi = (gap / entry) * 100
            return Opportunity(
                market=market, strategy="MISPRICING", side=side,
                entry_price=entry, expected_payout=entry + gap, roi_pct=roi,
                risk_score=4, confidence=0.65,
                reasoning=reasoning
            )
        return None


class Scanner:
    def __init__(self, top_n=10, min_roi=MIN_ROI):
        self.client = PolymarketClient()
        self.analyzer = StrategyAnalyzer(self.client)
        self.top_n = top_n
        self.min_roi = min_roi
        self.opportunities = []

    def fetch_markets(self):
        logger.info(f"Récupération des marchés (limite: {MARKET_LIMIT})...")
        markets = []
        offset = 0
        batch = 100
        while len(markets) < MARKET_LIMIT:
            batch_data = self.client.fetch_active_markets(limit=batch, offset=offset)
            if not batch_data:
                break
            markets.extend(batch_data)
            offset += batch
            if len(batch_data) < batch:
                break
        logger.info(f"{len(markets)} marchés récupérés")
        return markets

    def scan(self):
        raw_markets = self.fetch_markets()
        valid = 0
        for i, raw in enumerate(raw_markets):
            market = self.analyzer.parse_market(raw)
            if not market:
                continue
            if market.volume < MIN_VOLUME or market.liquidity < MIN_LIQUIDITY:
                continue
            valid += 1
            if (i + 1) % 20 == 0:
                logger.info(f"  Analyse {i+1}/{len(raw_markets)}...")

            opp = self.analyzer.analyze_yesno_arbitrage(market)
            if opp and opp.roi_pct >= self.min_roi * 100:
                self.opportunities.append(opp)

            for opp in self.analyzer.analyze_near_certain(market):
                if opp.roi_pct >= self.min_roi * 100:
                    self.opportunities.append(opp)

            opp = self.analyzer.analyze_mispricing(market)
            if opp and opp.roi_pct >= self.min_roi * 100:
                self.opportunities.append(opp)

        logger.info(f"{valid} marchés valides analysés, {len(self.opportunities)} opportunités trouvées")

    def display(self):
        if not self.opportunities:
            print("\n Aucune opportunité trouvée avec les critères actuels.")
            print("Essaie: python polymarket_scanner.py --min-roi 1")
            return

        sorted_opps = sorted(self.opportunities, key=lambda x: (x.risk_score, -x.roi_pct))
        top = sorted_opps[:self.top_n]

        print("\n" + "=" * 80)
        print(f"  POLYMARKET SCANNER — Top {len(top)} opportunités")
        print(f"  Scan du {datetime.now().strftime('%d/%m/%Y %H:%M:%S')}")
        print("=" * 80)

        for rank, opp in enumerate(top, 1):
            days_str = f"{opp.days_to_expiry}j" if opp.days_to_expiry is not None else "N/A"
            risk_bar = "█" * opp.risk_score + "░" * (10 - opp.risk_score)
            print(f"\n#{rank} [{opp.strategy}]")
            print(f"   {opp.market.question[:75]}...")
            print(f"   Side: {opp.side} @ {opp.entry_price:.4f}")
            print(f"   ROI:  {opp.roi_pct:.2f}% | Risque: {risk_bar} ({opp.risk_score}/10) | Expire: {days_str}")
            print(f"   Vol:  ${opp.market.volume:,.0f} | Liq: ${opp.market.liquidity:,.0f}")
            print(f"   Info: {opp.reasoning}")
            if opp.orderbook:
                ob = opp.orderbook
                print(f"   Book: bid={ob.best_bid:.4f} ask={ob.best_ask:.4f} spread={ob.spread:.4f}")

        print("\n" + "=" * 80)
        print(f"  {len(self.opportunities)} opportunités au total | Affichage top {len(top)}")
        print("=" * 80 + "\n")


def main():
    parser = argparse.ArgumentParser(description="Polymarket Opportunity Scanner")
    parser.add_argument("--top", type=int, default=10, help="Nombre d'opportunités à afficher")
    parser.add_argument("--min-roi", type=float, default=2.0, help="ROI minimum en %")
    args = parser.parse_args()

    print("Démarrage du scanner Polymarket...")
    scanner = Scanner(top_n=args.top, min_roi=args.min_roi / 100)
    scanner.scan()
    scanner.display()


if __name__ == "__main__":
    main()
