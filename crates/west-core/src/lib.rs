use std::fmt;

pub const WEST_DIR: &str = ".west";

#[derive(Debug, Clone, Copy, PartialEq, Eq)]
pub struct WestNotFound;

impl fmt::Display for WestNotFound {
    fn fmt(&self, f: &mut fmt::Formatter<'_>) -> fmt::Result {
        f.write_str("could not find a west workspace in this or any parent directory")
    }
}

impl std::error::Error for WestNotFound {}

pub mod config;
pub mod config_paths;
pub mod topdir;
