pub const WEST_DIR: &str = ".west";

#[derive(Debug, Clone, Copy, PartialEq, Eq, thiserror::Error)]
#[error("could not find a west workspace in this or any parent directory")]
pub struct WestNotFound;

pub mod config;
pub mod config_paths;
pub mod manifest;
pub mod topdir;
pub mod vcs;
