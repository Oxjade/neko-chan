//! Phase 6 normalize writer core (shared lib): decoded events → normalized
//! PG rows. Calls the `neko-indexer` binary wrappers for nothing — this module
//! only needs a Postgres transaction, the resolver, and checkpoint meta.
//!
//! Structural idempotency, not trust: natural-key `ON CONFLICT DO NOTHING`
//! makes replay converge. Money stays `numeric` from exact decimal strings;
//! jsonb values are bound as text and cast, so nothing crosses a float.

use anyhow::{Context, Result};
use serde_json::json;
use sqlx::Postgres;
use sqlx::Transaction;
use sui_rpc::proto::sui::rpc::v2::Event;

use crate::adapt::{
    decode, map_trade, numeric_equal, proto_value_to_json, ty_from_event_type, PackageResolver,
    Side, TokenTrade, Venue,
};

/// Where a normalized row lands, by venue.
#[derive(Debug, Clone, Copy, PartialEq, Eq)]
pub enum Table {
    BondingTrades,
    Swaps,
}

pub struct TradeMeta {
    pub tx_digest: String,
    pub event_index: i32,
    pub checkpoint: i64,
    pub ts_ms: i64,
}

/// Outcome of one checkpoint's normalization (for the caller's counters).
#[derive(Debug, Clone, Copy, Default)]
pub struct Written {
    pub trades: u64,
    pub dlq: u64,
}

/// Decode + normalize every adapted event in a checkpoint and persist trades
/// and any DLQ lines in one transaction. Non-adapted venues are passthrough.
pub async fn publish_checkpoint(
    tx: &mut Transaction<'_, Postgres>,
    res: &PackageResolver,
    checkpoint: i64,
    ts_ms: i64,
    events: &[Event],
) -> Result<Written> {
    let mut out = Written::default();
    for ev in events {
        let Some(event_type) = ev.event_type.clone() else { continue };
        if Venue::from_event_type(&event_type).is_none() {
            continue;
        }
        let meta = TradeMeta {
            tx_digest: ev.transaction_digest.clone().unwrap_or_default(),
            event_index: ev.event_index.unwrap_or(0) as i32,
            checkpoint,
            ts_ms,
        };

        let (raw_bcs, server_json) = event_bytes(ev);
        let decoded = match ty_from_event_type(&event_type) {
            Ok(root) => match decode(root, &raw_bcs, res).await {
                Ok(v) => v,
                Err(e) => {
                    push_dlq_in(
                        tx,
                        "live",
                        &meta,
                        &event_type,
                        server_json.unwrap_or(serde_json::Value::Null),
                        format!("{e:#}"),
                    )
                    .await?;
                    out.dlq += 1;
                    continue;
                }
            },
            Err(e) => {
                push_dlq_in(
                    tx,
                    "live",
                    &meta,
                    &event_type,
                    server_json.unwrap_or(serde_json::Value::Null),
                    format!("{e:#}"),
                )
                .await?;
                out.dlq += 1;
                continue;
            }
        };

        match map_trade(&event_type, &decoded) {
            Ok(Some(trade)) => {
                // Parity guard only where it can hurt: we are about to persist
                // canonical columns derived from OUR decode. Non-trade events
                // stay in raw_checkpoints regardless, so a mirror diff on a
                // passthrough is noise — not a DLQ item (§24).
                let mismatch = server_json
                    .as_ref()
                    .map(|j| !numeric_equal(&decoded, j))
                    .unwrap_or(false);
                if mismatch {
                    push_dlq_in(
                        tx,
                        "live",
                        &meta,
                        &event_type,
                        json!({"decoded": decoded, "node_json": server_json}),
                        "decode != node json mirror".to_string(),
                    )
                    .await?;
                    out.dlq += 1;
                    continue;
                }
                upsert_trade_in(tx, &meta, &trade).await?;
                out.trades += 1;
            }
            Ok(None) => {}
            Err(e) => {
                push_dlq_in(tx, "live", &meta, &event_type, decoded, format!("{e:#}")).await?;
                out.dlq += 1;
            }
        }
    }
    Ok(out)
}

fn event_bytes(ev: &Event) -> (Vec<u8>, Option<serde_json::Value>) {
    let bcs = ev
        .contents
        .as_ref()
        .and_then(|b| b.value.as_ref())
        .cloned()
        .map(|b| b.to_vec())
        .unwrap_or_default();
    let json = ev
        .json
        .as_ref()
        .map(|j| proto_value_to_json(j.as_ref()));
    (bcs, json)
}

fn table_for(venue: Venue) -> Result<Table> {
    Ok(match venue {
        Venue::SuiPump => Table::BondingTrades,
        Venue::Cetus => Table::Swaps,
    })
}

async fn upsert_trade_in(
    tx: &mut Transaction<'_, Postgres>,
    meta: &TradeMeta,
    trade: &TokenTrade,
) -> Result<()> {
    match table_for(trade.venue)? {
        Table::BondingTrades => {
            let side = trade
                .side
                .map(|s| match s {
                    Side::Buy => "buy",
                    Side::Sell => "sell",
                })
                .context("bond trade missing side")?;
            let amount_sui = trade.amount_sui.clone().context("bond trade missing amount_sui")?;
            let amount_token = trade.amount_token.clone().context("bond trade missing amount_token")?;
            sqlx::query(
                r#"
                INSERT INTO bonding_trades
                    (tx_digest, event_index, checkpoint, ts_ms, curve_id, side, wallet,
                     token, amount_sui, amount_token, fees, snapshot, fields, raw, event_type)
                VALUES ($1,$2,$3,$4,$5,$6,$7,$8,$9::numeric,$10::numeric,
                        $11::jsonb,$12::jsonb,$13::jsonb,$14::jsonb,$15)
                ON CONFLICT (tx_digest, event_index) DO NOTHING
                "#,
            )
            .bind(&meta.tx_digest)
            .bind(meta.event_index)
            .bind(meta.checkpoint)
            .bind(meta.ts_ms)
            .bind(&trade.entity)
            .bind(side)
            .bind(&trade.wallet)
            .bind(Option::<&str>::None)
            .bind(&amount_sui)
            .bind(&amount_token)
            .bind(serde_json::to_string(&trade.fees)?)
            .bind(serde_json::to_string(&trade.snapshot)?)
            .bind(serde_json::to_string(&trade.fields)?)
            .bind(serde_json::to_string(&trade.raw)?)
            .bind(&trade.event_type)
            .execute(&mut **tx)
            .await
            .context("upsert bonding_trades")?;
        }
        Table::Swaps => {
            let amount_in = trade.amount_in.clone().context("swap missing amount_in")?;
            let amount_out = trade.amount_out.clone().context("swap missing amount_out")?;
            sqlx::query(
                r#"
                INSERT INTO swaps
                    (tx_digest, event_index, checkpoint, ts_ms, dex, pool, atob,
                     amount_in, amount_out, fee, fields, raw, event_type)
                VALUES ($1,$2,$3,$4,$5,$6,$7,$8::numeric,$9::numeric,$10::numeric,
                        $11::jsonb,$12::jsonb,$13)
                ON CONFLICT (tx_digest, event_index) DO NOTHING
                "#,
            )
            .bind(&meta.tx_digest)
            .bind(meta.event_index)
            .bind(meta.checkpoint)
            .bind(meta.ts_ms)
            .bind("cetus")
            .bind(&trade.entity)
            .bind(trade.fields.get("atob").and_then(|v| v.as_bool()))
            .bind(&amount_in)
            .bind(&amount_out)
            .bind(trade.fees.get("fee_amount").and_then(|v| v.as_str()))
            .bind(serde_json::to_string(&trade.fields)?)
            .bind(serde_json::to_string(&trade.raw)?)
            .bind(&trade.event_type)
            .execute(&mut **tx)
            .await
            .context("upsert swaps")?;
        }
    }
    Ok(())
}

pub async fn push_dlq_in(
    tx: &mut Transaction<'_, Postgres>,
    source: &str,
    meta: &TradeMeta,
    event_type: &str,
    payload: serde_json::Value,
    reason: String,
) -> Result<()> {
    sqlx::query(
        r#"
        INSERT INTO dlq (source, checkpoint, event_type, tx_digest, event_index, payload, reason)
        VALUES ($1, $2, $3, $4, $5, $6::jsonb, $7)
        "#,
    )
    .bind(source)
    .bind(meta.checkpoint)
    .bind(event_type)
    .bind(&meta.tx_digest)
    .bind(meta.event_index)
    .bind(serde_json::to_string(&payload)?)
    .bind(reason)
    .execute(&mut **tx)
    .await
    .context("insert dlq")?;
    Ok(())
}