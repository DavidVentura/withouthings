//! End to end: how many exceptions the watch takes to reach the point where WPP
//! is up. A wait that fails to sleep still boots and passes every other test, and
//! shows up only as an exception count far beyond what booting needs.

mod common;

use std::collections::BTreeMap;
use std::fs;
use std::ops::RangeInclusive;
use std::path::Path;
use std::time::Duration;

use common::{holds_whole_prompt, relink, renode_binary, repository, require, scratch, Renode, Rig};

const BOOT_TIMEOUT: Duration = Duration::from_secs(180);
const SUPERVISOR_CALL: &str = "SuperVisorCall";
/// Wide enough for timing to move the counts, narrow enough that a spinning
/// wait or a boot that takes no interrupts falls outside.
const BOOT_EXCEPTIONS: RangeInclusive<usize> = 1_000..=8_000;
const BOOT_SUPERVISOR_CALLS: RangeInclusive<usize> = 20..=500;
/// Exceptions taken a handful of times can differ by a few between runs.
const SMALL_COUNT_SLACK: usize = 8;
const ACKNOWLEDGED: &str = "nvic: Acknowledged IRQ ";

/// How many times each exception was taken, by the NVIC's name for it.
fn acknowledged(log: &str) -> BTreeMap<String, usize> {
    let mut counts = BTreeMap::new();
    for line in log.lines() {
        let Some((_, name)) = line.split_once(ACKNOWLEDGED) else {
            continue;
        };
        let name = name.split('\u{1b}').next().unwrap().trim_end_matches('.');
        *counts.entry(name.to_string()).or_insert(0) += 1;
    }
    counts
}

fn exceptions_during_boot(name: &str, rig: &Rig) -> BTreeMap<String, usize> {
    let directory = scratch(name);
    rig.generate(&directory, "rig");
    fs::write(
        directory.join("run.resc"),
        format!(
            "$image=@{}\ninclude @scripts/wpp-pipe.resc\nlogLevel -1 sysbus.nvic\n",
            rig.image.display()
        ),
    )
    .expect("the run script is writable");
    let renode = Renode::start(&directory, "include @run.resc");
    renode.wait_for("Add WPPS chars.", BOOT_TIMEOUT).expect("the watch boots");
    renode.monitor("pause");
    drain_log(&directory);
    let counts = acknowledged(&fs::read_to_string(directory.join("log")).expect("Renode keeps a log"));
    drop(renode);
    println!("{name}: {counts:?}");
    counts
}

/// Renode's logging thread runs behind the CPU.
fn drain_log(directory: &Path) {
    let size = || fs::metadata(directory.join("log")).expect("Renode keeps a log").len();
    let mut seen = size();
    loop {
        std::thread::sleep(Duration::from_millis(500));
        let now = size();
        if now == seen {
            return;
        }
        seen = now;
    }
}

fn assert_boot_is_in_range(image: &str, counts: &BTreeMap<String, usize>) {
    let total: usize = counts.values().sum();
    assert!(
        BOOT_EXCEPTIONS.contains(&total),
        "the {image} image took {total} exceptions to boot, outside {BOOT_EXCEPTIONS:?}: {counts:?}"
    );
    assert!(
        counts.get(SUPERVISOR_CALL).is_some_and(|calls| BOOT_SUPERVISOR_CALLS.contains(calls)),
        "the {image} image's supervisor calls are outside {BOOT_SUPERVISOR_CALLS:?}: {counts:?}"
    );
}

/// Both images are the same application, so a kernel built from source has no
/// reason to take any exception much more or less often than the stock one.
fn assert_same_exceptions(stock: &BTreeMap<String, usize>, relinked: &BTreeMap<String, usize>) {
    for name in stock.keys().chain(relinked.keys()) {
        let [from_stock, from_relinked] = [stock, relinked].map(|counts| counts.get(name).copied().unwrap_or(0));
        assert!(
            from_relinked <= from_stock * 2 + SMALL_COUNT_SLACK
                && from_stock <= from_relinked * 2 + SMALL_COUNT_SLACK,
            "{name} was taken {from_stock} times by stock and {from_relinked} by relinked"
        );
    }
}

#[test]
fn a_prompt_is_not_whole_until_its_colour_reset_has_arrived() {
    assert!(!holds_whole_prompt(b"0x0000270F\r\n\x1b[33m(hwa10) \x1b"));
    assert!(holds_whole_prompt(b"0x0000270F\r\n\x1b[33m(hwa10) \x1b[0m"));
}

#[test]
#[ignore = "needs the Renode rig and ~/ref-build; run with --ignored"]
fn booting_takes_a_bounded_number_of_exceptions() {
    require(&[
        ("Renode", &renode_binary()),
        ("the flash dump", &repository().join("renode-sim/external_flash.bin")),
    ]);
    relink();
    let stock = exceptions_during_boot("boot-stock-test", &Rig::stock());
    let relinked = exceptions_during_boot("boot-relinked-test", &Rig::relinked());
    assert_boot_is_in_range("stock", &stock);
    assert_boot_is_in_range("relinked", &relinked);
    assert_same_exceptions(&stock, &relinked);
}
