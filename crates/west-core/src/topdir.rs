use crate::{WEST_DIR, WestNotFound};
use std::path::{Path, PathBuf};

use log::debug;

pub fn topdir(start: impl AsRef<Path>) -> Result<PathBuf, WestNotFound> {
    let mut cur = start.as_ref().to_path_buf();
    loop {
        debug!("checking {}", cur.display());
        if cur.join(WEST_DIR).is_dir() {
            return Ok(cur);
        }
        if !cur.pop() {
            return Err(WestNotFound);
        }
    }
}

#[cfg(test)]
mod tests {
    use std::fs;

    use tempfile::TempDir;

    use super::*;

    fn workspace() -> TempDir {
        let tmp = tempfile::tempdir().expect("create tempdir");
        fs::create_dir(tmp.path().join(WEST_DIR)).expect("create .west");
        tmp
    }

    #[test]
    fn finds_west_in_start_dir() {
        let ws = workspace();
        assert_eq!(topdir(ws.path()).unwrap(), ws.path());
    }

    #[test]
    fn finds_west_in_parent_dir() {
        let ws = workspace();
        let nested = ws.path().join("a/b/c");
        fs::create_dir_all(&nested).unwrap();
        assert_eq!(topdir(&nested).unwrap(), ws.path());
    }

    #[test]
    fn returns_err_when_no_west_dir() {
        // No `.west` anywhere in or above this tempdir.
        let tmp = tempfile::tempdir().unwrap();
        assert_eq!(topdir(tmp.path()), Err(WestNotFound));
    }

    #[test]
    fn ignores_west_file_that_is_not_a_directory() {
        let tmp = tempfile::tempdir().unwrap();
        fs::write(tmp.path().join(WEST_DIR), b"not a dir").unwrap();
        assert_eq!(topdir(tmp.path()), Err(WestNotFound));
    }

    #[test]
    fn accepts_str_path_and_pathbuf() {
        let ws = workspace();
        let as_str: &str = ws.path().to_str().unwrap();
        let as_path: &Path = ws.path();
        let as_pathbuf: PathBuf = ws.path().to_path_buf();

        assert_eq!(topdir(as_str).unwrap(), ws.path());
        assert_eq!(topdir(as_path).unwrap(), ws.path());
        assert_eq!(topdir(as_pathbuf).unwrap(), ws.path());
    }
}
