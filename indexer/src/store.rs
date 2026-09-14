use anyhow::{Context, Result};
use sqlx::postgres::PgPoolOptions;
use sqlx::{PgPool, Postgres, Transaction};

pub async fn connect(database_url: &str) -> Result<PgPool> {
    let pool = PgPoolOptions::new()
        .max_connections(5)
        .connect(database_url)
        .await
        .context("connect to postgres")?;
    sqlx::migrate!("./migrations")
        .run(&pool)
        .await
        .context("run sqlx migrations")?;
    Ok(pool)
}

/// A single checkpoint row, persisted idempotently.
pub struct RawCheckpoint {
    pub seq: i64,
    pub digest: String,
    pub previous_digest: Option<String>,
    pub epoch: i64,
    pub ts_ms: i64,
    pub network_total_transactions: Option<i64>,
    pub data: Vec<u8>,
}

impl RawCheckpoint {
    pub async fn insert(
        &self,
        tx: &mut Transaction<'_, Postgres>,
    ) -> Result<bool> {
        let res = sqlx::query(
            r#"
            INSERT INTO raw_checkpoints
                (seq, digest, previous_digest, epoch, ts_ms, network_total_transactions, data)
            VALUES ($1, $2, $3, $4, $5, $6, $7)
            ON CONFLICT (seq) DO NOTHING
            "#,
        )
        .bind(self.seq)
        .bind(&self.digest)
        .bind(&self.previous_digest)
        .bind(self.epoch)
        .bind(self.ts_ms)
        .bind(self.network_total_transactions)
        .bind(&self.data)
        .execute(&mut **tx)
        .await
        .context("insert raw_checkpoints")?;
        Ok(res.rows_affected() > 0)
    }
}

pub async fn save_progress_in(
    tx: &mut Transaction<'_, Postgres>,
    name: &str,
    last_seq: i64,
) -> Result<()> {
    sqlx::query(
        r#"
        INSERT INTO checkpoint_progress (name, last_seq, updated_at)
        VALUES ($1, $2, now())
        ON CONFLICT (name) DO UPDATE SET last_seq = EXCLUDED.last_seq, updated_at = now()
        "#,
    )
    .bind(name)
    .bind(last_seq)
    .execute(&mut **tx)
    .await
    .context("save checkpoint progress")?;
    Ok(())
}

pub async fn load_progress(pool: &PgPool, name: &str) -> Result<Option<i64>> {
    let row: Option<(i64,)> =
        sqlx::query_as("SELECT last_seq FROM checkpoint_progress WHERE name = $1")
            .bind(name)
            .fetch_optional(pool)
            .await
            .context("load checkpoint progress")?;
    Ok(row.map(|r| r.0))
}