//! End-to-end smoke test for the layered config store.
//!
//! Run with: `cargo run -p west-core --example config_smoke`
//!
//! Builds a two-layer stack in a tempdir, sets values on each layer,
//! drops/reloads, then verifies merged + per-layer reads. Useful for manual
//! verification until the user-facing `west config` subcommand lands.

use std::error::Error;

use tempfile::TempDir;
use west_core::config::{ConfigValue, Configuration};

fn main() -> Result<(), Box<dyn Error>> {
    let tmp = TempDir::new()?;
    let lower = tmp.path().join("lower.toml");
    let upper = tmp.path().join("upper.toml");

    // First pass: write some values, then drop the Configuration.
    {
        let mut cfg = Configuration::load([lower.clone(), upper.clone()])?;
        cfg.set(
            "manifest.path",
            ConfigValue::String("zephyr".into()),
            &lower,
        )?;
        cfg.set(
            "manifest.file",
            ConfigValue::String("west.yml".into()),
            &lower,
        )?;
        cfg.set("update.narrow", ConfigValue::Bool(true), &upper)?;
        cfg.set(
            "manifest.path",
            ConfigValue::String("override".into()),
            &upper,
        )?;
        println!("--- written ---");
        println!("{}:\n{}", lower.display(), std::fs::read_to_string(&lower)?);
        println!("{}:\n{}", upper.display(), std::fs::read_to_string(&upper)?);
    }

    // Second pass: reload from disk and verify.
    let cfg = Configuration::load([lower.clone(), upper.clone()])?;

    println!("--- merged read ---");
    println!("manifest.path = {:?}", cfg.get_str("manifest.path"));
    println!("manifest.file = {:?}", cfg.get_str("manifest.file"));
    println!("update.narrow = {:?}", cfg.get_bool("update.narrow")?);

    println!("--- per-layer read ---");
    println!(
        "lower manifest.path = {:?}",
        cfg.get_str_in("manifest.path", &lower)?
    );
    println!(
        "upper manifest.path = {:?}",
        cfg.get_str_in("manifest.path", &upper)?
    );

    println!("--- merged items ---");
    for (k, v) in cfg.items() {
        println!("  {k} = {v:?}");
    }

    assert_eq!(cfg.get_str("manifest.path").as_deref(), Some("override"));
    assert_eq!(cfg.get_str("manifest.file").as_deref(), Some("west.yml"));
    assert_eq!(cfg.get_bool("update.narrow")?, Some(true));

    println!("\nOK");
    Ok(())
}
