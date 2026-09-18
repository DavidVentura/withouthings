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
/// Distinct from TEST_VERSION so a relinked run cannot be mistaken for a stock one.
const RELINKED_VERSION: u32 = 9998;
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

/// One `0x…` word out of a monitor answer.
fn word(answer: String) -> u64 {
    let text = answer.trim();
    u64::from_str_radix(text.trim_start_matches("0x").trim_start_matches("0X"), 16)
        .unwrap_or_else(|_| panic!("the monitor answered {text:?}, not a word"))
}

/// The address an ELF32-LE symbol table gives a name.
///
/// The relinked library's statics are laid out by the linker, so where
/// xTickCount lands moves whenever the kernel or the port changes size. Reading
/// it out of the ELF is the only form of the address that stays true.
fn symbol_address(elf: &Path, want: &str) -> u64 {
    let data = std::fs::read(elf).expect("the relinked ELF is readable");
    let u32_at = |at: usize| u32::from_le_bytes(data[at..at + 4].try_into().unwrap());
    let u16_at = |at: usize| u16::from_le_bytes(data[at..at + 2].try_into().unwrap());
    let headers = u32_at(0x20) as usize;
    let entry = u16_at(0x2E) as usize;
    let count = u16_at(0x30) as usize;
    for index in 0..count {
        let header = headers + index * entry;
        if u32_at(header + 4) != 2 {
            continue; // SHT_SYMTAB
        }
        let table = u32_at(header + 0x10) as usize;
        let size = u32_at(header + 0x14) as usize;
        let strings = u32_at(headers + u32_at(header + 0x18) as usize * entry + 0x10) as usize;
        for at in (table..table + size).step_by(16) {
            let name = strings + u32_at(at) as usize;
            let end = data[name..].iter().position(|&b| b == 0).unwrap() + name;
            if &data[name..end] == want.as_bytes() {
                return u32_at(at + 4) as u64;
            }
        }
    }
    panic!("{} defines no {want}", elf.display());
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

/// The image a rig is generated for: the internal-flash binary the addresses
/// are read out of, and where the symbol table to resolve them against lives
/// (`partition` for a stock image, which has no ELF).
struct Rig {
    image: PathBuf,
    symbols: String,
}

impl Rig {
    fn stock() -> Rig {
        Rig {
            image: repository().join("renode-sim/flash.bin"),
            symbols: "partition".to_string(),
        }
    }

    fn relinked() -> Rig {
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
    fn generate(&self, directory: &Path, into: &str) {
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

/// One update scenario: which flash image the watch starts from, what package
/// is pushed, and what the watch must report before and after.
struct Case {
    /// The monitor's `$image`, or None for `boot.resc`'s own default.
    image: Option<PathBuf>,
    /// The `appl` part to package, or None to keep the stock package's.
    appl: Option<PathBuf>,
    /// The rig for the image the run boots, and the one for the image the
    /// update installs; they differ exactly when the two layouts differ.
    boot: Rig,
    target: Rig,
    installed_version: u32,
    target_version: u32,
}

impl Case {
    /// The stock case, with every field overridable from the environment so the
    /// same test can be pointed at another image without editing it.
    fn from_environment() -> Case {
        let number = |name: &str, fallback: u32| {
            std::env::var(name)
                .map(|v| v.parse().expect("the version in the environment is a number"))
                .unwrap_or(fallback)
        };
        let rig = |image: &str, symbols: &str| match std::env::var(image) {
            Err(_) => Rig::stock(),
            Ok(path) => Rig {
                image: PathBuf::from(path),
                symbols: std::env::var(symbols).unwrap_or_else(|_| "partition".to_string()),
            },
        };
        Case {
            image: std::env::var("UPDATE_TEST_IMAGE").ok().map(PathBuf::from),
            appl: std::env::var("UPDATE_TEST_APPL").ok().map(PathBuf::from),
            boot: rig("UPDATE_TEST_IMAGE", "UPDATE_TEST_SYMBOLS"),
            target: rig("UPDATE_TEST_TARGET_IMAGE", "UPDATE_TEST_TARGET_SYMBOLS"),
            installed_version: number("UPDATE_TEST_INSTALLED_VERSION", INSTALLED_VERSION),
            target_version: number("UPDATE_TEST_VERSION", TEST_VERSION),
        }
    }
}

/// Renode's monitor port and the WPP pipe port are fixed, so the scenarios run
/// one at a time.
static ONE_AT_A_TIME: std::sync::Mutex<()> = std::sync::Mutex::new(());

/// Build the package and return where the image in it reads its own version.
///
/// The trailer is a movable section, so mkpkg.py resolves it against the linked
/// ELF of the part it is packaging and prints the address it wrote to; the
/// client is told the same address so that what it checks the package against
/// and what the watch will report are the same word.
fn mkpkg(repository: &Path, case: &Case, source: &Path, package: &Path) -> String {
    let mut command = Command::new("python3");
    command
        .arg(repository.join("tools/mkpkg.py"))
        .args(["--version", &case.target_version.to_string()]);
    if let Some(appl) = &case.appl {
        command.arg("--appl").arg(appl);
        assert_ne!(
            case.target.symbols, "partition",
            "a package with its own appl part needs the ELF that part was linked from"
        );
        command.arg("--symbols").arg(&case.target.symbols);
    }
    let built = command
        .arg(source)
        .arg(package)
        .output()
        .expect("python3 runs");
    assert!(
        built.status.success(),
        "mkpkg.py failed: {}",
        String::from_utf8_lossy(&built.stderr)
    );
    let output = String::from_utf8_lossy(&built.stdout).to_string();
    print!("{output}");
    output
        .lines()
        .find_map(|line| line.strip_prefix("appl_version_address "))
        .expect("mkpkg.py prints where it wrote the version trailer")
        .trim()
        .to_string()
}

/// Push `case`'s package into a watch booted from `case`'s image and return the
/// running Renode plus its scratch directory once the new version is up.
fn run_update(case: &Case) -> Option<(Renode, PathBuf)> {
    let repository = repository();
    let renode = renode_binary();
    let dump = repository.join("renode-sim/external_flash.bin");
    let source = repository.join("hwa10_3411_Tf4fD4.bin");
    for (what, path) in [("Renode", &renode), ("the flash dump", &dump), ("the package", &source)] {
        if !path.exists() {
            println!("skipping: {what} is not at {}", path.display());
            return None;
        }
    }

    let directory = scratch(&repository.join("renode-sim"));
    case.boot.generate(&directory, "rig");
    case.target.generate(&directory, "rig-updated");
    let package = directory.join(format!("hwa10_{}.bin", case.target_version));
    let version_address = mkpkg(&repository, case, &source, &package);
    if case.appl.is_none() {
        assert_eq!(
            fs::metadata(&package).unwrap().len(),
            fs::metadata(&source).unwrap().len(),
            "a version-only package must be the same length as the one it came from"
        );
    }

    let script = match &case.image {
        Some(image) => format!("$image=@{}; include @update-test.resc", image.display()),
        None => "include @update-test.resc".to_string(),
    };
    let log = fs::File::create(directory.join("log")).unwrap();
    let mut child = Command::new(&renode)
        .current_dir(&directory)
        .args(["--disable-xwt", "--port", &MONITOR_PORT.to_string(), "-e"])
        .arg(&script)
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
    assert_eq!(reported_version(&output), case.installed_version);

    let (updated, output) = client(
        &directory,
        &["--update", package.to_str().unwrap(), "--version-address", &version_address],
    );
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
        case.target_version,
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
    println!("bank 1 appl version: {}", renode.monitor("sysbus.spi2 ReadImageWord 0x6014"));
    println!("bank 2 appl version: {}", renode.monitor("sysbus.spi2 ReadImageWord 0x11f014"));
    let protected = renode.monitor("sysbus.nvmc VerifyProtectedRegion");
    assert_eq!(protected, "0x00000000", "the MBR and SoftDevice took a stray store");
    // The word the running image's getter reads, wherever the layout put it:
    // the version the probe already reported has to be in flash at that address
    // and not at whatever address the stock image used.
    let trailer = word(renode.monitor(&format!("sysbus ReadDoubleWord {version_address}")));
    assert_eq!(
        trailer, case.target_version as u64,
        "the version trailer at {version_address} holds {trailer}, not the version the watch reports"
    );
    println!("appl version word in flash at {version_address}: {trailer}");
    Some((renode, directory))
}

#[test]
fn an_update_pushed_over_wpp_boots_and_reports_its_new_version() {
    let _serial = ONE_AT_A_TIME.lock().unwrap_or_else(|e| e.into_inner());
    let started = Instant::now();
    let case = Case::from_environment();
    let Some((_renode, _directory)) = run_update(&case) else {
        return;
    };
    println!("wall time: {:.1}s", started.elapsed().as_secs_f64());
}

/// The same path with the relinked image: a package whose `appl` part is the
/// app blob plus the FreeRTOS built from source, which is 0x22b8 bytes longer
/// than the stock part.
#[test]
fn the_relinked_image_installs_through_the_update_path() {
    let _serial = ONE_AT_A_TIME.lock().unwrap_or_else(|e| e.into_inner());
    let started = Instant::now();
    let repository = repository();
    let relink = Command::new(repository.join("renode-sim/abi/relink.sh"))
        .output()
        .expect("abi/relink.sh runs");
    if !relink.status.success() {
        println!(
            "skipping: abi/relink.sh needs ~/ref-build:\n{}",
            String::from_utf8_lossy(&relink.stderr)
        );
        return;
    }
    print!("{}", String::from_utf8_lossy(&relink.stdout));

    let appl = repository.join("renode-sim/out/appl-relinked.bin");
    let case = Case {
        image: None,            // the update starts from the stock image
        appl: Some(appl.clone()),
        boot: Rig::stock(),
        target: Rig::relinked(),
        installed_version: INSTALLED_VERSION,
        target_version: RELINKED_VERSION,
    };
    let Some((renode, _directory)) = run_update(&case) else {
        return;
    };

    // Each bank is the package verbatim, so its fwblk header's appl entry has
    // the part length at +0xc and the version at +0x14 (FIRMWARE.md).
    let length = fs::metadata(&appl).unwrap().len() as u64;
    let banks = [
        word(renode.monitor("sysbus.spi2 ReadImageWord 0x600c")),
        word(renode.monitor("sysbus.spi2 ReadImageWord 0x11f00c")),
    ];
    assert!(
        banks.contains(&length),
        "no bank carries the relinked appl part's {length:#x} bytes: {banks:x?}"
    );

    // The blob's own kernel must be dead in the image that came out of the
    // update: its tick never moves, and the source kernel's does. The blob's is
    // at 0x20021920 (abi/boundary.yaml); the source kernel's comes out of the
    // ELF, because the linker decides where it goes.
    assert_eq!(
        renode.monitor("sysbus ReadDoubleWord 0x20021920"),
        "0x00000000",
        "the blob's FreeRTOS is the one keeping time"
    );
    let ticks = symbol_address(
        &repository.join("renode-sim/out/relink/relinked.elf"),
        "xTickCount",
    );
    let read = format!("sysbus ReadDoubleWord {ticks:#x}");
    let first = renode.monitor(&read);
    std::thread::sleep(Duration::from_secs(3));
    let second = renode.monitor(&read);
    assert_ne!(first, second, "the source kernel's tick is not advancing ({first})");
    println!("source kernel tick {first} -> {second}");
    println!("wall time: {:.1}s", started.elapsed().as_secs_f64());
}
