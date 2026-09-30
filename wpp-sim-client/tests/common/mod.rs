//! The Renode rig as a test fixture: a scratch copy of renode-sim, the
//! simulated watch started headless in it, and the monitor and console
//! reached from outside.

#![allow(dead_code)]

use std::fs;
use std::io::{Read, Write};
use std::net::TcpStream;
use std::path::{Path, PathBuf};
use std::process::{Child, ChildStdin, Command, Stdio};
use std::time::{Duration, Instant};

pub const MONITOR_PORT: u16 = 7799;
const SHELL_ECHO_TIMEOUT: Duration = Duration::from_secs(60);
const SHELL_QUEUE_BYTES: usize = 8;

/// A monitor connection held open for many commands whose answers nobody reads.
/// Renode takes the lines in order, so the only wait is for the socket to take
/// the bytes.
struct MonitorSession {
    socket: TcpStream,
}

impl MonitorSession {
    fn open() -> MonitorSession {
        let socket = TcpStream::connect(("127.0.0.1", MONITOR_PORT))
            .expect("the Renode monitor accepts a connection");
        socket.set_nodelay(true).unwrap();
        MonitorSession { socket }
    }

    fn send(&mut self, command: &str) {
        self.socket.write_all(format!("{command}\n").as_bytes()).unwrap();
    }
}

pub struct Renode {
    child: Child,
    _stdin: Option<ChildStdin>,
    pub directory: PathBuf,
}

impl Drop for Renode {
    fn drop(&mut self) {
        let _ = self.child.kill();
        let _ = self.child.wait();
    }
}

impl Renode {
    /// Renode headless on `script`, with its monitor on MONITOR_PORT and its
    /// output in `directory`'s log.
    pub fn start(directory: &Path, script: &str) -> Renode {
        let log = fs::File::create(directory.join("log")).unwrap();
        let mut child = Command::new(renode_binary())
            .current_dir(directory)
            .args(["--disable-xwt", "--port", &MONITOR_PORT.to_string(), "-e"])
            .arg(script)
            // Renode exits when its input closes, so the handle is held for the run.
            .stdin(Stdio::piped())
            .stdout(Stdio::from(log.try_clone().unwrap()))
            .stderr(Stdio::from(log))
            .spawn()
            .expect("Renode starts");
        let stdin = child.stdin.take();
        Renode { child, _stdin: stdin, directory: directory.to_path_buf() }
    }

    pub fn wait_for(&self, needle: &str, timeout: Duration) -> Result<(), String> {
        self.wait_for_count(needle, 1, timeout)
    }

    pub fn wait_for_count(&self, needle: &str, count: usize, timeout: Duration) -> Result<(), String> {
        let deadline = Instant::now() + timeout;
        while Instant::now() < deadline {
            if self.log().matches(needle).count() >= count {
                return Ok(());
            }
            std::thread::sleep(Duration::from_secs(1));
        }
        Err(format!(
            "the watch did not print {needle:?} {count} times within {}s; the log ends with:\n{}",
            timeout.as_secs(),
            tail(&self.log(), 40)
        ))
    }

    /// One line at the debug console, paced by the echo: the reader's queue
    /// (the xQueueCreate at 0x59f66) holds eight bytes, so a line is written at
    /// most a queue's worth at a time and the next part waits until the line
    /// editor has echoed the last. The terminator is CR, since the editor's
    /// jump table at 0x59dcc ignores LF.
    pub fn shell(&self, line: &str) {
        let mut monitor = MonitorSession::open();
        let typed_from = self.log().len();
        let mut echoed = String::new();
        for part in line.as_bytes().chunks(SHELL_QUEUE_BYTES) {
            for byte in part {
                monitor.send(&format!("sysbus.uart0 WriteChar {byte}"));
            }
            echoed.push_str(std::str::from_utf8(part).unwrap());
            let deadline = Instant::now() + SHELL_ECHO_TIMEOUT;
            while !self.log().get(typed_from..).unwrap_or("").contains(&echoed) {
                assert!(
                    Instant::now() < deadline,
                    "the console never echoed {echoed:?}; the log ends with:\n{}",
                    tail(&self.log(), 20)
                );
                std::thread::sleep(Duration::from_millis(20));
            }
        }
        monitor.send("sysbus.uart0 WriteChar 13");
    }

    pub fn log(&self) -> String {
        String::from_utf8_lossy(&fs::read(self.directory.join("out/uart0.log")).unwrap_or_default())
            .into_owned()
    }

    pub fn wait_for_boots(&self, boots: usize, timeout: Duration) -> Result<(), String> {
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
    pub fn monitor(&self, command: &str) -> String {
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

pub fn strip_colour(line: &str) -> String {
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

pub fn tail(text: &str, lines: usize) -> String {
    let all: Vec<&str> = text.lines().collect();
    all[all.len().saturating_sub(lines)..].join("\n")
}

pub fn repository() -> PathBuf {
    Path::new(env!("CARGO_MANIFEST_DIR")).parent().unwrap().to_path_buf()
}

pub fn renode_binary() -> PathBuf {
    match std::env::var("RENODE") {
        Ok(path) => PathBuf::from(path),
        Err(_) => PathBuf::from(std::env::var("HOME").unwrap_or_default())
            .join("renode-portable/renode"),
    }
}

/// A scratch copy of the rig: Renode writes its outputs next to the scripts it
/// runs, and the models and images are large, so everything is symlinked and
/// only the scripts are copied.
///
/// Renode resolves every `@path` against the working directory and not against
/// the file that names it, so the copy reproduces renode-sim's directory shape:
/// scripts/ is a real directory of copied scripts and models/ is one symlink.
pub fn scratch(name: &str) -> PathBuf {
    let source = repository().join("renode-sim");
    let directory = repository().join("target").join(name);
    let _ = fs::remove_dir_all(&directory);
    fs::create_dir_all(directory.join("out/frames")).expect("the scratch directory is creatable");
    fs::create_dir_all(directory.join("scripts")).expect("the scratch directory is creatable");
    for entry in fs::read_dir(source.join("scripts")).expect("renode-sim/scripts is readable") {
        let entry = entry.unwrap();
        fs::copy(entry.path(), directory.join("scripts").join(entry.file_name()))
            .expect("the script copies");
    }
    for entry in fs::read_dir(source).expect("renode-sim is readable") {
        let entry = entry.unwrap();
        let name = entry.file_name();
        if name == "out" || name == "scripts" {
            continue;
        }
        let target = directory.join(&name);
        std::os::unix::fs::symlink(entry.path(), &target).expect("the link is creatable");
    }
    directory
}

pub fn client(directory: &Path, arguments: &[&str]) -> (bool, String) {
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

/// The image a rig is generated for: the internal-flash binary the addresses
/// are read out of, and where the symbol table to resolve them against lives
/// (`partition` for a stock image, which has no ELF).
pub struct Rig {
    pub image: PathBuf,
    pub symbols: String,
}

impl Rig {
    pub fn stock() -> Rig {
        Rig {
            image: repository().join("renode-sim/flash.bin"),
            symbols: "partition".to_string(),
        }
    }

    pub fn relinked() -> Rig {
        Rig {
            image: repository().join("renode-sim/out/flash-relinked.bin"),
            symbols: repository()
                .join("renode-sim/out/relink/relinked.elf")
                .display()
                .to_string(),
        }
    }

    /// Resolve abi/sim.yaml against this image and write the fragments the run
    /// scripts include, into the scratch rig rather than into the source tree.
    pub fn generate(&self, directory: &Path, into: &str) {
        let repository = repository();
        let generated = Command::new("python3")
            .arg(repository.join("renode-sim/abi/rig.py"))
            .arg("--image")
            .arg(&self.image)
            .args(["--symbols", &self.symbols])
            .arg("--out")
            .arg(directory.join("out").join(into))
            .output()
            .expect("python3 runs");
        assert!(
            generated.status.success(),
            "abi/rig.py refused the {into} addresses: {}",
            String::from_utf8_lossy(&generated.stderr)
        );
        print!("{}", String::from_utf8_lossy(&generated.stdout));
    }
}

