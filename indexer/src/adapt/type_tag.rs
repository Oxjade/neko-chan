//! Move type-tag parsing: `0xADDR::module::Name<...>` / `vector<T>`.
//!
//! Used to turn a full event type string (and its type arguments) into a
//! structure we can resolve against on-chain datatype descriptors.
//! Never invents type names — it only parses strings the network emitted.

use anyhow::{bail, Result};

#[derive(Debug, Clone, PartialEq)]
pub enum Kind {
    Struct,
    Vector,
    Prim,
}

const PRIMITIVES: &[&str] = &[
    "bool", "u8", "u16", "u32", "u64", "u128", "u256", "address",
];

#[derive(Debug, Clone, PartialEq)]
pub struct TypeTagStr {
    pub kind: Kind,
    /// Address (hex, lowercase) — empty for `vector`.
    pub address: String,
    pub module: String,
    pub name: String,
    pub args: Vec<TypeTagStr>,
}

impl TypeTagStr {
    /// Plain `address::module::name` (no type args) used for descriptor lookup.
    pub fn plain(&self) -> String {
        match self.kind {
            Kind::Vector => "vector".to_string(),
            Kind::Prim => self.name.clone(),
            Kind::Struct => format!("{}::{}::{}", self.address, self.module, self.name),
        }
    }

    /// Full re-serialization including type args.
    pub fn to_full(&self) -> String {
        match self.kind {
            Kind::Vector => format!("vector<{}>", self.args[0].to_full()),
            Kind::Prim => self.name.clone(),
            Kind::Struct => {
                if self.args.is_empty() {
                    self.plain()
                } else {
                    let inner = self
                        .args
                        .iter()
                        .map(|a| a.to_full())
                        .collect::<Vec<_>>()
                        .join(", ");
                    format!("{}<{inner}>", self.plain())
                }
            }
        }
    }
}

fn split_top(s: &str) -> Result<Vec<&str>> {
    let mut out = Vec::new();
    let mut depth = 0usize;
    let mut start = 0usize;
    for (i, c) in s.char_indices() {
        match c {
            '<' => depth += 1,
            '>' => depth = depth.saturating_sub(1),
            ',' if depth == 0 => {
                out.push(s[start..i].trim());
                start = i + 1;
            }
            _ => {}
        }
    }
    out.push(s[start..].trim());
    Ok(out)
}

pub fn parse(s: &str) -> Result<TypeTagStr> {
    let s = s.trim();
    if s.is_empty() {
        bail!("empty type tag");
    }
    if PRIMITIVES.contains(&s) {
        return Ok(TypeTagStr {
            kind: Kind::Prim,
            address: String::new(),
            module: String::new(),
            name: s.to_string(),
            args: Vec::new(),
        });
    }
    if let Some(rest) = s.strip_prefix("vector<") {
        let inner = rest
            .strip_suffix('>')
            .ok_or_else(|| anyhow::anyhow!("unterminated vector< in {s}"))?;
        let args = split_top(inner)?;
        if args.len() != 1 {
            bail!("vector requires exactly one type arg");
        }
        return Ok(TypeTagStr {
            kind: Kind::Vector,
            address: String::new(),
            module: String::new(),
            name: String::new(),
            args: vec![parse(args[0])?],
        });
    }

    // Split off type args at top level.
    let (head, arg_str) = match s.find('<') {
        Some(lt) => {
            let close = s.rfind('>').ok_or_else(|| anyhow::anyhow!("unterminated <> in {s}"))?;
            (&s[..lt], Some(&s[lt + 1..close]))
        }
        None => (s, None),
    };

    let parts: Vec<&str> = head.split("::").collect();
    if parts.len() != 3 {
        bail!("expected address::module::Name, got {s}");
    }
    let args = match arg_str {
        None => Vec::new(),
        Some(inner) => split_top(inner)?.into_iter().map(parse).collect::<Result<_>>()?,
    };

    Ok(TypeTagStr {
        kind: Kind::Struct,
        address: parts[0].to_lowercase(),
        module: parts[1].to_string(),
        name: parts[2].to_string(),
        args,
    })
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn parses_simple() {
        let t = parse("0x25ebb9a7c50eb17b3fa9c5a30fb8b5ad8f97caaf4928943acbcff7153dfee5e3::pool::SwapEvent").unwrap();
        assert_eq!(t.module, "pool");
        assert_eq!(t.name, "SwapEvent");
        assert_eq!(t.args.len(), 0);
    }

    #[test]
    fn parses_nested() {
        let t = parse("0x1::option::Option<vector<u64>>").unwrap();
        assert_eq!(t.args.len(), 1);
        assert_eq!(t.args[0].kind, Kind::Vector);
        assert_eq!(t.args[0].args[0].kind, Kind::Prim);
        assert_eq!(t.args[0].args[0].name, "u64");
    }

    #[test]
    fn parses_primitives() {
        assert_eq!(parse("u64").unwrap().kind, Kind::Prim);
        assert_eq!(parse("bool").unwrap().name, "bool");
        assert!(parse("u512").is_err());
    }

    #[test]
    fn parses_address() {
        let t = parse("0x228cbec8a6b0555ae316f5957befe945cbdc5e9f5894d4a0bd3c60308a492b5::template::TEMPLATE")
            .unwrap();
        assert_eq!(t.module, "template");
        assert_eq!(t.name, "TEMPLATE");
        assert!(t.address.starts_with("0x"));
        assert!(t.address.chars().skip(2).all(|c| c.is_ascii_hexdigit()));
    }

    #[test]
    fn rejects_bad() {
        assert!(parse("not/a/type").is_err());
    }
}