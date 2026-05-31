//! Structured-data format selector shared by every west file that
//! supports multiple serialisations.
//!
//! `Format` is the tag the manifest loader (`manifest.rs`) and the
//! west-commands loader (`west_commands.rs`) use to route a body
//! string to the right serde parser. Centralising the enum here
//! keeps the extension-dispatch and the YAML-as-default convention
//! in one place; both modules call into the same
//! [`Format::from_extension`] helper.
//!
//! v1 west was YAML-only; v2 added TOML and JSON to the manifest +
//! west-commands surface. Both formats share the same logical schema
//! per file type — the format just changes the serialisation skin.

/// Which structured-data format a body should be parsed as.
#[derive(Debug, Clone, Copy, PartialEq, Eq)]
pub enum Format {
    Yaml,
    Toml,
    Json,
}

impl Format {
    /// Resolve a file extension (lowercase, no leading dot) to a
    /// format. `.yml` / `.yaml` / no-extension → YAML (matches v1's
    /// `west.yml` default and v2's authoring convention); `.toml` →
    /// TOML; `.json` → JSON; anything else returns `None` so the
    /// caller can surface the offending extension verbatim.
    pub fn from_extension(ext: Option<&str>) -> Option<Self> {
        match ext {
            Some("yaml") | Some("yml") | None => Some(Self::Yaml),
            Some("toml") => Some(Self::Toml),
            Some("json") => Some(Self::Json),
            _ => None,
        }
    }

    /// Uppercase human-readable label for use in error messages
    /// (e.g. `"YAML parse error: …"`). Picks the canonical
    /// spelling each ecosystem uses for itself.
    pub fn name(self) -> &'static str {
        match self {
            Self::Yaml => "YAML",
            Self::Toml => "TOML",
            Self::Json => "JSON",
        }
    }
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn extension_maps_known_formats() {
        assert_eq!(Format::from_extension(Some("yaml")), Some(Format::Yaml));
        assert_eq!(Format::from_extension(Some("yml")), Some(Format::Yaml));
        assert_eq!(Format::from_extension(Some("toml")), Some(Format::Toml));
        assert_eq!(Format::from_extension(Some("json")), Some(Format::Json));
    }

    #[test]
    fn no_extension_defaults_to_yaml() {
        // Matches v1's `west.yml`-style file naming where the .yml
        // suffix was the de-facto default; v2 preserves that.
        assert_eq!(Format::from_extension(None), Some(Format::Yaml));
    }

    #[test]
    fn unknown_extension_returns_none() {
        assert_eq!(Format::from_extension(Some("xml")), None);
        assert_eq!(Format::from_extension(Some("ini")), None);
    }

    #[test]
    fn name_returns_canonical_uppercase_label() {
        assert_eq!(Format::Yaml.name(), "YAML");
        assert_eq!(Format::Toml.name(), "TOML");
        assert_eq!(Format::Json.name(), "JSON");
    }
}
