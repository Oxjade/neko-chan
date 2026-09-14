use anyhow::{Context, Result};

/// Re-exported so binaries can name the concrete client type.
pub use sui_rpc::Client;

/// Shared gRPC v2 client construction (TLS + optional x-sui-chain-id guard).
pub fn connect(endpoint: &str, chain_id: Option<&str>) -> Result<Client> {
    let mut client = Client::new(endpoint).context("construct sui gRPC v2 client")?;
    if let Some(chain_id) = chain_id {
        if !chain_id.is_empty() {
            let mut headers = sui_rpc::client::HeadersInterceptor::new();
            let value = tonic::metadata::MetadataValue::try_from(chain_id.to_string())
                .map_err(|e| anyhow::anyhow!("bad x-sui-chain-id header: {e}"))?;
            headers
                .headers_mut()
                .insert(sui_rpc::headers::X_SUI_CHAIN_ID, value);
            client = client.with_headers(headers);
        }
    }
    Ok(client)
}