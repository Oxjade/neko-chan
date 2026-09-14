//! G4 mapping layer: decoded event -> canonical trade row.
//!
//! Rules (spec §24, DESIGN §10):
//! - Never invent semantics: `Side`/venue come from the event name and fields
//!   the network emitted, verbatim.
//! - Money stays exact: every quantity is carried as a decimal-string of raw
//!   units; this layer performs NO arithmetic (no floats, no derived price).
//! - Unknown trades error (caller routes to DLQ); non-trade adapted events
//!   (fees, comments, payouts, buybacks) return `Ok(None)` so the raw row is
//!   persisted alone — nothing is guessed into a trade.

use anyhow::{bail, Context, Result};
use serde_json::{Map, Value};

/// Venue of an event, derived from the *defining* package id embedded in its
/// type string (stable across package upgrades; see DESIGN §5).
#[derive(Debug, Clone, Copy, PartialEq, Eq)]
pub enum Venue {
    SuiPump,
    Cetus,
}

impl Venue {
    pub fn from_event_type(event_type: &str) -> Option<Venue> {
        if event_type.starts_with(SUIPUMP_DEFINE) {
            Some(Venue::SuiPump)
        } else if event_type.starts_with(CETUS_DEFINE) {
            Some(Venue::Cetus)
        } else {
            None
        }
    }
}

/// Defining (original) package ids — the stable event-source identity
/// (`event_type` prefix), never the storage id.
pub const SUIPUMP_DEFINE: &str = "0x7b4163d17ce18b386ee50929ba48fa0a2ecb60304df4b07e26835aa18617cda2";
pub const SUIPUMP_BONDING_MODULE: &str = "bonding_curve";
pub const CETUS_DEFINE: &str = "0x1eabed72c53feb3805120a081dc15963c204dc8d091542592abaf7a35689b2fb";
pub const CETUS_POOL_MODULE: &str = "pool";

#[derive(Debug, Clone, Copy, PartialEq, Eq)]
pub enum Side {
    Buy,
    Sell,
}

/// Canonical, fully-exact trade row (feeds `bonding_trades` / `swaps` in §10).
#[derive(Debug, Clone)]
pub struct TokenTrade {
    pub venue: Venue,
    pub side: Option<Side>,
    /// Bond curve object id (SuiPump) or pool object id (Cetus).
    pub entity: String,
    /// Wallet that traded (SuiPump buyer/seller). Cetus `SwapEvent` carries no
    /// trader field, so wallet is only set for bond curves.
    pub wallet: Option<String>,
    /// Raw base-token units (SuiPump).
    pub amount_token: Option<String>,
    /// Raw SUI units (SuiPump).
    pub amount_sui: Option<String>,
    /// Raw amounts for the dex path (Cetus amount_in / amount_out).
    pub amount_in: Option<String>,
    pub amount_out: Option<String>,
    /// Fee fields, verbatim (SuiPump: airdrop/creator/lp/protocol/referral fee
    /// and tail_refund; Cetus: fee_amount).
    pub fees: Map<String, Value>,
    /// Post-trade curve snapshot (new_sui_reserve / new_token_reserve /
    /// grad_threshold_used).
    pub snapshot: Map<String, Value>,
    /// Every remaining literal field from the decoded event.
    pub fields: Map<String, Value>,
    /// The full decoded event (audit / raw re-derivation).
    pub raw: Value,
    pub event_type: String,
}

/// Map a decoded event to a canonical trade. `Ok(None)` = adapted venue but
/// not a trade (raw passthrough); `Err` = trade event missing a required
/// field — never guess (→ DLQ).
pub fn map_trade(event_type: &str, decoded: &Value) -> Result<Option<TokenTrade>> {
    let venue = match Venue::from_event_type(event_type) {
        Some(v) => v,
        None => return Ok(None),
    };
    let short = event_type
        .rsplit(':')
        .next()
        .with_context(|| format!("event type has no name: {event_type}"))?;
    let obj = decoded
        .as_object()
        .with_context(|| format!("{short} not an object"))?;

    let mut trade = TokenTrade {
        venue,
        side: None,
        entity: String::new(),
        wallet: None,
        amount_token: None,
        amount_sui: None,
        amount_in: None,
        amount_out: None,
        fees: Map::new(),
        snapshot: Map::new(),
        fields: obj.clone(),
        raw: decoded.clone(),
        event_type: event_type.to_string(),
    };

    match venue {
        Venue::SuiPump => {
            if event_type
                .strip_prefix(&format!("{SUIPUMP_DEFINE}::{SUIPUMP_BONDING_MODULE}::"))
                .is_some()
            {
                if trading_bonds(&mut trade, short, obj)? == false {
                    return Ok(None);
                }
            } else {
                return Ok(None);
            }
        }
        Venue::Cetus => {
            if event_type
                .strip_prefix(&format!("{CETUS_DEFINE}::{CETUS_POOL_MODULE}::"))
                .is_none()
            {
                return Ok(None);
            }
            if short != "SwapEvent" {
                return Ok(None);
            }
            trade.entity = get_addr(obj, "pool")?;
            trade.amount_in = Some(get_money(obj, "amount_in")?);
            trade.amount_out = Some(get_money(obj, "amount_out")?);
            if let Some(v) = obj.get("fee_amount") {
                trade.fees.insert("fee_amount".to_string(), v.clone());
            }
            // Direction is the network's own field; the Cetus coin-identity
            // mapping is out of scope for this layer, so buy/sell is unset.
            let consumed = ["pool", "amount_in", "amount_out", "fee_amount"];
            for k in consumed {
                trade.fields.remove(k);
            }
        }
    }

    // Money is exact strings of raw units; a float anywhere is a decoder bug
    // and must not reach the canonical row.
    assert_no_numbers_map(&trade.fields)?;
    assert_no_numbers_map(&trade.fees)?;
    assert_no_numbers_map(&trade.snapshot)?;
    Ok(Some(trade))
}

/// Returns `Ok(true)` when `short` is a bond trade (fields populated), or
/// `Ok(false)` for other bonding-curve events (raw passthrough only).
fn trading_bonds(trade: &mut TokenTrade, short: &str, obj: &Map<String, Value>) -> Result<bool> {
    match short {
        "TokensPurchased" => {
            trade.side = Some(Side::Buy);
            trade.wallet = Some(get_addr(obj, "buyer")?);
            trade.amount_sui = Some(get_money(obj, "sui_in")?);
            trade.amount_token = Some(get_money(obj, "tokens_out")?);
        }
        "TokensSold" => {
            trade.side = Some(Side::Sell);
            trade.wallet = Some(get_addr(obj, "seller")?);
            trade.amount_sui = Some(get_money(obj, "sui_out")?);
            trade.amount_token = Some(get_money(obj, "tokens_in")?);
        }
        _ => return Ok(false),
    }
    trade.entity = get_addr(obj, "curve_id")?;
    for fee in [
        "airdrop_fee",
        "creator_fee",
        "lp_fee",
        "protocol_fee",
        "referral_fee",
        "tail_refund",
    ] {
        if let Some(v) = obj.get(fee) {
            trade.fees.insert(fee.to_string(), v.clone());
        }
    }
    for snap in ["new_sui_reserve", "new_token_reserve", "grad_threshold_used"] {
        if let Some(v) = obj.get(snap) {
            trade.snapshot.insert(snap.to_string(), v.clone());
        }
    }
    let consumed = [
        "buyer", "seller", "curve_id", "sui_in", "sui_out", "tokens_in", "tokens_out",
        "airdrop_fee", "creator_fee", "lp_fee", "protocol_fee", "referral_fee",
        "tail_refund", "new_sui_reserve", "new_token_reserve", "grad_threshold_used",
    ];
    for k in consumed {
        trade.fields.remove(k);
    }
    Ok(true)
}

fn get_money<'a>(obj: &'a Map<String, Value>, key: &str) -> Result<String> {
    match obj.get(key) {
        Some(Value::String(s)) if !s.is_empty() && s.chars().all(|c| c.is_ascii_digit()) => Ok(s.clone()),
        other => bail!("{key}: expected exact decimal string, got {other:?}"),
    }
}

fn get_addr(obj: &Map<String, Value>, key: &str) -> Result<String> {
    match obj.get(key) {
        Some(Value::String(s)) if s.starts_with("0x") && s.len() == 66 => Ok(s.clone()),
        other => bail!("{key}: expected 0x address string, got {other:?}"),
    }
}

fn assert_no_numbers_map(m: &Map<String, Value>) -> Result<()> {
    for v in m.values() {
        assert_no_numbers(v)?;
    }
    Ok(())
}

fn assert_no_numbers(v: &Value) -> Result<()> {
    match v {
        Value::Object(m) => assert_no_numbers_map(m),
        Value::Array(items) => {
            for item in items {
                assert_no_numbers(item)?;
            }
            Ok(())
        }
        Value::Number(_) => bail!("float/number in canonical trade row (money must be strings)"),
        _ => Ok(()),
    }
}

#[cfg(test)]
mod tests {
    use super::*;
    use crate::adapt::replay::{CETUS_SWAP_FIXTURES, FixtureFile, SUIPUMP_EVENT_FIXTURES};

    fn run_cases(json: &str) -> Vec<TokenTrade> {
        let fx: FixtureFile = serde_json::from_str(json).expect("fixture parses");
        let mut trades = Vec::new();
        for ev in &fx.events {
            if let Some(t) = map_trade(&ev.event_type, &ev.server_json).expect("maps") {
                trades.push(t);
            }
        }
        trades
    }

    #[test]
    fn suipump_bonding_trades_map() {
        let trades = run_cases(SUIPUMP_EVENT_FIXTURES);
        let buys: Vec<_> = trades.iter().filter(|t| t.side == Some(Side::Buy)).collect();
        let sells: Vec<_> = trades.iter().filter(|t| t.side == Some(Side::Sell)).collect();
        assert!(!buys.is_empty(), "must contain buys");
        assert!(!sells.is_empty(), "must contain sells");

        for t in &trades {
            assert_eq!(t.venue, Venue::SuiPump);
            assert!(t.entity.starts_with("0x") && t.entity.len() == 66, "curve id");
            assert!(matches!(t.wallet.as_deref(), Some(w) if w.starts_with("0x") && w.len() == 66));
            let (sui, tok) = (t.amount_sui.as_ref().unwrap(), t.amount_token.as_ref().unwrap());
            assert!(sui.chars().all(|c| c.is_ascii_digit()));
            assert!(tok.chars().all(|c| c.is_ascii_digit()));
            assert_ne!(tok, "0", "trade must move tokens");
            // Post-trade snapshot present for both sides.
            assert!(t.snapshot.contains_key("new_sui_reserve"));
            assert!(t.snapshot.contains_key("new_token_reserve"));
            // Every fee field the network emitted is kept verbatim.
            for k in ["airdrop_fee", "creator_fee", "lp_fee", "protocol_fee"] {
                assert!(t.fees.contains_key(k), "missing {k}");
            }
        }
        assert_eq!(buys.len() + sells.len(), trades.len(), "no other suipump event maps as a trade");
    }

    #[test]
    fn suipump_non_trades_pass_through() {
        let fx: FixtureFile = serde_json::from_str(SUIPUMP_EVENT_FIXTURES).unwrap();
        let mut seen_non_trade = false;
        for ev in &fx.events {
            let short = ev.event_type.rsplit(':').next().unwrap();
            if matches!(short, "Comment" | "CreatorFeesClaimed" | "ProtocolFeesClaimed"
                | "PayoutsUpdated" | "BuybackExecuted")
            {
                seen_non_trade = true;
                assert!(
                    map_trade(&ev.event_type, &ev.server_json).unwrap().is_none(),
                    "{short} must not map to a trade"
                );
            }
        }
        assert!(seen_non_trade, "fixture must exercise non-trade events");
    }

    #[test]
    fn cetus_swaps_map() {
        let trades = run_cases(CETUS_SWAP_FIXTURES);
        assert!(!trades.is_empty());
        for t in &trades {
            assert_eq!(t.venue, Venue::Cetus);
            assert_eq!(t.side, None, "dex side not invented");
            assert!(t.entity.starts_with("0x") && t.entity.len() == 66, "pool id");
            let (a, b) = (t.amount_in.as_ref().unwrap(), t.amount_out.as_ref().unwrap());
            assert!(a.chars().all(|c| c.is_ascii_digit()));
            assert!(b.chars().all(|c| c.is_ascii_digit()));
            assert_ne!(a, "0");
            assert_ne!(b, "0");
            assert!(t.fees.contains_key("fee_amount"));
            // Direction flag preserved from the network.
            assert!(t.fields.contains_key("atob"));
        }
    }

    #[test]
    fn canonical_rows_have_no_numbers() {
        for t in run_cases(SUIPUMP_EVENT_FIXTURES) {
            assert_no_numbers(&t.raw).unwrap();
        }
        for t in run_cases(CETUS_SWAP_FIXTURES) {
            assert_no_numbers(&t.raw).unwrap();
        }
        // The raw mirror may be anything; ensure the *fast* fields really ride
        // on strings by checking the whole decoded fixture goes through
        // assert_no_numbers as object too.
        let fx: FixtureFile = serde_json::from_str(SUIPUMP_EVENT_FIXTURES).unwrap();
        for ev in &fx.events {
            assert_no_numbers(&ev.server_json).unwrap_or_else(|e| panic!("{}: {e}", ev.event_type));
        }
    }
}