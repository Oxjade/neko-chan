//! On-chain datatype resolver + cache over MovePackageService::GetDatatype.
//!
//! Layouts are authoritative chain state, not a local re-declaration: an
//! event is only decoded after we fetch the exact struct descriptor the
//! network reports for it. Nothing here guesses field order or width.

use std::collections::HashMap;

use anyhow::{Context, Result};
use sui_rpc::proto::sui::rpc::v2::{
    DatatypeDescriptor, GetDatatypeRequest, GetDatatypeResponse, GetPackageRequest,
    GetPackageResponse,
};
use tokio::sync::Mutex;

pub use sui_rpc::client::Client;

use crate::adapt::type_tag::{Kind, TypeTagStr};

/// Resolves `<address>::<module>::<Name>` against the network's
/// MovePackageService, memoizing every descriptor we have already seen.
#[derive(Clone)]
pub struct PackageResolver {
    client: Client,
    cache: std::sync::Arc<Mutex<HashMap<String, std::sync::Arc<DatatypeDescriptor>>>>,
}

impl PackageResolver {
    pub fn new(client: Client) -> Self {
        Self {
            client,
            cache: Default::default(),
        }
    }

    pub fn client(&self) -> &Client {
        &self.client
    }

    /// Seed the layout cache from recorded chain state (fixtures/replay).
    /// Enables deterministic, network-free decode tests: the same bytes +
    /// same authoritative layout must produce the same result.
    pub async fn seed(&self, key: String, desc: DatatypeDescriptor) {
        self.cache.lock().await.insert(key, std::sync::Arc::new(desc));
    }

    /// Fetch a datatype descriptor by its fully-qualified plain name
    /// (`address::module::Name`, with or without type arguments).
    pub async fn datatype(&self, tag: &TypeTagStr) -> Result<std::sync::Arc<DatatypeDescriptor>> {
        if tag.kind == Kind::Vector {
            anyhow::bail!("cannot fetch a layout for vector ({}); {}", tag.plain(), tag.to_full());
        }
        let key = tag.plain();
        if let Some(hit) = self.cache.lock().await.get(&key) {
            return Ok(hit.clone());
        }

        let mut client = self.client.clone();
        let mut mover = client.package_client();
        let mut request = GetDatatypeRequest::default();
        request.package_id = Some(tag.address.clone());
        request.module_name = Some(tag.module.clone());
        request.name = Some(tag.name.clone());
        let response: GetDatatypeResponse = mover
            .get_datatype(request)
            .await
            .context("GetDatatype rpc")?
            .into_inner();

        let desc = response
            .datatype
            .context("GetDatatype returned empty datatype (module or type may not exist)")?;
        let cached = std::sync::Arc::new(desc);
        self.cache.lock().await.insert(key, cached.clone());
        Ok(cached)
    }

    /// Raw MovePackageService::GetPackage for module/datatype enumeration.
    pub async fn package(&self, package_id: &str) -> Result<sui_rpc::proto::sui::rpc::v2::Package> {
        let mut client = self.client.clone();
        let mut mover = client.package_client();
        let mut request = GetPackageRequest::default();
        request.package_id = Some(package_id.to_string());
        let response: GetPackageResponse = mover
            .get_package(request)
            .await
            .context("GetPackage rpc")?
            .into_inner();
        response.package.context("GetPackage returned empty package")
    }
}

/// Parse a leading `0x`-prefixed address string into its raw 32 bytes and
/// lowercase hex. Used by fixtures incl. sender/pool addresses.
pub fn address_from_hex(s: &str) -> Result<String> {
    let mut t = s.strip_prefix("0x").unwrap_or(s).to_lowercase();
    while t.len() < 64 {
        t.insert(0, '0');
    }
    if t.len() != 64 || !t.chars().all(|c| c.is_ascii_hexdigit()) {
        anyhow::bail!("invalid address {s}");
    }
    Ok(format!("0x{t}"))
}

/// Keep the import list honest (proto field helpers used by verify).
#[allow(dead_code)]
fn touch_getters(r: &GetDatatypeResponse) -> Option<String> {
    r.datatype.as_ref().and_then(|d| d.type_name.clone())
}