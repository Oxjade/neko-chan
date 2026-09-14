mod config;
mod metrics;
mod rpc;
mod store;
mod stream;

use std::sync::Arc;

use anyhow::Result;

#[tokio::main]
async fn main() -> Result<()> {
    tracing_subscriber::fmt()
        .with_env_filter(
            tracing_subscriber::EnvFilter::try_from_default_env()
                .unwrap_or_else(|_| "info".into()),
        )
        .init();

    let settings = config::load()?;
    tracing::info!(endpoint = %settings.endpoint, "neko-indexer (phase 1: raw checkpoint intake)");

    let pool = store::connect(&settings.database_url).await?;

    let metrics = Arc::new(metrics::Metrics::default());

    let metrics_task = {
        let metrics = Arc::clone(&metrics);
        let interval = settings.metrics_interval;
        tokio::spawn(async move {
            metrics::periodic_log(metrics, interval).await;
        })
    };

    stream::run(&settings, pool, metrics).await?;

    metrics_task.abort();
    Ok(())
}