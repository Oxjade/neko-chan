use std::env;

use anyhow::{Context, Result};

#[derive(Debug, Clone)]
pub enum CheckpointStart {
    /// Start live at the chain tip.
    Tip,
    /// Resume from the persisted cursor in the database.
    Db,
    /// Start at an explicit sequence number.
    Seq(u64),
}

#[derive(Debug, Clone)]
pub struct Settings {
    pub endpoint: String,
    pub chain_id: Option<String>,
    pub database_url: String,
    pub start: CheckpointStart,
    pub metrics_interval: tokio::time::Duration,
}

pub fn load() -> Result<Settings> {
    let _ = dotenvy::dotenv();

    let start = match env::var("CHECKPOINT_START").unwrap_or_else(|_| "db".into()).as_str() {
        "tip" => CheckpointStart::Tip,
        "db" => CheckpointStart::Db,
        other => other
            .parse::<u64>()
            .map(CheckpointStart::Seq)
            .with_context(|| format!("invalid CHECKPOINT_START value: {other} (expected tip|db|<seq>)"))?,
    };

    Ok(Settings {
        endpoint: env::var("SUI_ENDPOINT")
            .unwrap_or_else(|_| "https://grpc.mainnet.sui.io:443".into()),
        chain_id: env::var("SUI_CHAIN_ID").ok(),
        database_url: env::var("DATABASE_URL")
            .context("DATABASE_URL must be set (see .env.example)")?,
        start,
        metrics_interval: tokio::time::Duration::from_secs(
            env::var("METRICS_INTERVAL_SECS")
                .ok()
                .and_then(|v| v.parse().ok())
                .unwrap_or(10),
        ),
    })
}