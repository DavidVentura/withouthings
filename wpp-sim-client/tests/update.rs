//! End to end: push a firmware package over WPP into the simulated watch, let
//! the bootloader apply the bank, and check that the image that comes back up
//! is the one that was pushed.
//!
//! The test drives the `wpp-sim-client` binary rather than its internals, so
//! what it exercises is the same thing a person would run, and reads the
//! watch's own log and the Renode monitor for everything the protocol does not
//! say. It skips, loudly, when Renode or the flash dump is not there.

use std::fs;
use std::io::{Read, Write};
use std::net::TcpStream;
use std::path::{Path, PathBuf};
use std::process::{Child, Command, Stdio};
use std::time::{Duration, Instant};

const MONITOR_PORT: u16 = 7799;
const BOOT_TIMEOUT: Duration = Duration::from_secs(180);
/// The bootloader compares and then copies the whole 1.15 MB bank over a
/// modelled SPI bus one byte at a time, which is minutes of wall time.
const UPDATE_BOOT_TIMEOUT: Duration = Duration::from_secs(1500);
const TEST_VERSION: u32 = 9999;
const INSTALLED_VERSION: u32 = 3411;

struct Renode {
    child: Child,
    directory: PathBuf,
}

impl Drop for Renode {
    fn drop(&mut self) {
        let _ = self.child.kill();
        let _ = self.child.wait();
    }
}

impl Renode {
    fn log(&self) -> String {
        fs::read_to_string(self.directory.join("out/uart0.log")).unwrap_or_default()
    }

    fn wait_for_boots(&self, boots: usize, timeout: Duration) -> Result<(), String> {
        let deadline = Instant::now() + timeout;
        while Instant::now() < deadline {
            if self.log().matches("Add WPPS chars.").count() >= boots {
                return Ok(());
            }
            std::thread::sleep(Duration::from_secs(2));
        }
        Err(format!(
            "the watch did not reach boot {boots} within {}s; the log ends with:\n{}",
            timeout.as_secs(),
            tail(&self.log(), 40)
        ))
    }

    /// One monitor command, one answer. Renode echoes the command before the
    /// result, and the telnet negotiation bytes at the start of the session are
    /// not text, so both are dropped.
    fn monitor(&self, command: &str) -> String {
        let mut socket = TcpStream::connect(("127.0.0.1", MONITOR_PORT))
            .expect("the Renode monitor accepts a connection");
        socket.set_read_timeout(Some(Duration::from_secs(10))).unwrap();
        std::thread::sleep(Duration::from_millis(400));
        let mut banner = [0u8; 4096];
        let _ = socket.read(&mut banner);
        socket.write_all(format!("{command}\n").as_bytes()).unwrap();
        std::thread::sleep(Duration::from_millis(800));
        let mut answer = [0u8; 4096];
        let read = socket.read(&mut answer).unwrap_or(0);
        String::from_utf8_lossy(&answer[..read])
            .lines()
            .map(|line| strip_colour(line).trim().to_string())
            .filter(|line| !line.is_empty() && line != command && !line.starts_with("(hwa10)"))
            .collect::<Vec<_>>()
            .join(" ")
    }
}

fn strip_colour(line: &str) -> String {
    let mut out = String::new();
    let mut characters = line.chars();
    while let Some(c) = characters.next() {
        if c != '\u{1b}' {
            out.push(c);
            continue;
        }
        for skip in characters.by_ref() {
            if skip == 'm' {
                break;
            }
        }
    }
    out
}

fn tail(text: &str, lines: usize) -> String {
    let all: Vec<&str> = text.lines().collect();
    all[all.len().saturating_sub(lines)..].join("\n")
}

fn repository() -> PathBuf {
    Path::new(env!("CARGO_MANIFEST_DIR")).parent().unwrap().to_path_buf()
}

fn renode_binary() -> PathBuf {
    match std::env::var("RENODE") {
        Ok(path) => PathBuf::from(path),
        Err(_) => PathBuf::from(std::env::var("HOME").unwrap_or_default())
            .join("renode-portable/renode"),
    }
}

/// A scratch copy of the rig: Renode writes its outputs next to the scripts it
/// runs, and the models and images are large, so everything is symlinked and
/// only the scripts are copied.
fn scratch(source: &Path) -> PathBuf {
    let directory = repository().join("target/update-test");
    let _ = fs::remove_dir_all(&directory);
    fs::create_dir_all(directory.join("out/frames")).expect("the scratch directory is creatable");
    for entry in fs::read_dir(source).expect("renode-sim is readable") {
        let entry = entry.unwrap();
        let name = entry.file_name();
        if name == "out" {
            continue;
        }
        let target = directory.join(&name);
        if entry.path().extension().is_some_and(|e| e == "resc") {
            fs::copy(entry.path(), &target).expect("the script copies");
        } else {
            std::os::unix::fs::symlink(entry.path(), &target).expect("the link is creatable");
        }
    }
    directory
}

fn client(directory: &Path, arguments: &[&str]) -> (bool, String) {
    let dump = repository().join("renode-sim/external_flash.bin");
    let mut command = Command::new(env!("CARGO_BIN_EXE_wpp-sim-client"));
    command
        .current_dir(directory)
        .arg("--secret-from-dump")
        .arg(&dump)
        .args(arguments)
        .stdout(Stdio::piped())
        .stderr(Stdio::piped());
    let child = command.spawn().expect("the client starts");
    let finished = child
        .wait_with_output()
        .expect("the client is waitable");
    let mut output = String::from_utf8_lossy(&finished.stdout).to_string();
    output.push_str(&String::from_utf8_lossy(&finished.stderr));
    (finished.status.success(), output)
}

fn reported_version(output: &str) -> u32 {
    output
        .lines()
        .find_map(|line| line.strip_prefix("soft_version="))
        .unwrap_or_else(|| panic!("the client reported no version:\n{}", tail(output, 30)))
        .trim()
        .parse()
        .expect("the reported version is a number")
}

#[test]
fn an_update_pushed_over_wpp_boots_and_reports_its_new_version() {
    let started = Instant::now();
    let repository = repository();
    let renode = renode_binary();
    let dump = repository.join("renode-sim/external_flash.bin");
    let source = repository.join("hwa10_3411_Tf4fD4.bin");
    for (what, path) in [("Renode", &renode), ("the flash dump", &dump), ("the package", &source)] {
        if !path.exists() {
            println!("skipping: {what} is not at {}", path.display());
            return;
        }
    }

    let directory = scratch(&repository.join("renode-sim"));
    let package = directory.join("hwa10_9999.bin");
    let built = Command::new("python3")
        .arg(repository.join("tools/mkpkg.py"))
        .args(["--version", &TEST_VERSION.to_string()])
        .arg(&source)
        .arg(&package)
        .output()
        .expect("python3 runs");
    assert!(
        built.status.success(),
        "mkpkg.py failed: {}",
        String::from_utf8_lossy(&built.stderr)
    );
    print!("{}", String::from_utf8_lossy(&built.stdout));
    assert_eq!(
        fs::metadata(&package).unwrap().len(),
        fs::metadata(&source).unwrap().len(),
        "the test package must be the same length as the one it came from"
    );

    let log = fs::File::create(directory.join("log")).unwrap();
    let mut child = Command::new(&renode)
        .current_dir(&directory)
        .args(["--disable-xwt", "--port", &MONITOR_PORT.to_string(), "-e"])
        .arg("include @update-test.resc")
        // Renode exits when its input closes, so the handle is held for the run.
        .stdin(Stdio::piped())
        .stdout(Stdio::from(log.try_clone().unwrap()))
        .stderr(Stdio::from(log))
        .spawn()
        .expect("Renode starts");
    let _stdin = child.stdin.take();
    let renode = Renode { child, directory: directory.clone() };

    renode.wait_for_boots(1, BOOT_TIMEOUT).expect("the watch boots");
    let (probed, output) = client(&directory, &["--probe-only"]);
    assert!(probed, "the first probe failed:\n{}", tail(&output, 30));
    assert_eq!(reported_version(&output), INSTALLED_VERSION);

    let (updated, output) = client(&directory, &["--update", package.to_str().unwrap()]);
    assert!(updated, "the update failed:\n{}", tail(&output, 30));
    assert!(
        renode.log().contains("[CHUNKED_UPDATE] chksum ok"),
        "the watch did not accept the digest:\n{}",
        tail(&renode.log(), 40)
    );

    renode
        .wait_for_boots(2, UPDATE_BOOT_TIMEOUT)
        .expect("the updated image boots");
    let (probed, output) = client(&directory, &["--probe-only"]);
    assert!(probed, "the probe after the update failed:\n{}", tail(&output, 30));
    assert_eq!(
        reported_version(&output),
        TEST_VERSION,
        "the watch is still running the old image"
    );

    let watch_log = renode.log();
    assert!(
        watch_log.contains("[FLASH BIN] FLASH OK"),
        "the bootloader never copied the bank into internal flash"
    );
    // The application's own reboot into the bootloader goes through its fault
    // handler, because the pipe has no answer for the disconnect the reboot
    // asks the SoftDevice for; what matters is that the image that comes out of
    // the update runs clean, so the scan starts where it took over.
    let after_update = watch_log.rsplit_once("boot attempt").map(|(_, rest)| rest).unwrap_or(&watch_log);
    assert!(!after_update.contains("Fault handler"), "the updated image faulted:\n{}", tail(after_update, 40));
    assert!(!after_update.contains("softdevice_assert"), "the SoftDevice asserted:\n{}", tail(after_update, 40));

    println!("boots: {}", watch_log.matches("[M] boot").count());
    for line in watch_log.lines().filter(|l| l.contains("boot reason")) {
        println!("{}", strip_colour(line).trim());
    }
    println!("appl version word in flash: {}", renode.monitor("sysbus ReadDoubleWord 0xf1178"));
    println!("bank 1 appl version: {}", renode.monitor("sysbus.spi2 ReadImageWord 0x6014"));
    println!("bank 2 appl version: {}", renode.monitor("sysbus.spi2 ReadImageWord 0x11f014"));
    let protected = renode.monitor("sysbus.nvmc VerifyProtectedRegion");
    assert_eq!(protected, "0x00000000", "the MBR and SoftDevice took a stray store");
    println!("wall time: {:.1}s", started.elapsed().as_secs_f64());
}
