fn main() {
    // Only relevant under `--features pyo3`: emit the
    // `-undefined dynamic_lookup` (macOS) / equivalent linker args
    // needed so the cdylib doesn't try to resolve python symbols at
    // link time. Without this, plain `cargo build --features pyo3` /
    // `cargo test --workspace` would fail to link the cdylib on macOS.
    if std::env::var_os("CARGO_FEATURE_PYO3").is_some() {
        pyo3_build_config::add_extension_module_link_args();
    }
}
