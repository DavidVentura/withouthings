//! End to end: episodes the watch stores on its own, between syncs, reach the
//! phone.
//!
//! The PPG AFib alert ("irregular rhythm, take an ECG") is kept on the watch as
//! a stored measure and handed over only when the phone asks for its stored
//! signal type by name. Nothing over WPP makes one, so the watch's own debug
//! command `hwa10 fake_afib` stores them, through the same store a detection
//! writes to.

mod common;

use std::time::Duration;

use common::{client, renode_binary, repository, scratch, tail, Renode, Rig};

const BOOT_TIMEOUT: Duration = Duration::from_secs(600);
const STORE_TIMEOUT: Duration = Duration::from_secs(120);
/// `[PPG_AFIB] AFIB %d/%d stored (diagnosis = %s | ...)`, once per episode kept.
const EPISODE_STORED: &str = " stored (diagnosis = ";

/// The console only runs when the debug cable's RX line idles high: 0x2e5e8
/// reads P0.08 and skips shell_init when it reads low.
const SCRIPT: &str = "include @scripts/wpp-pipe.resc; pause; sysbus.gpio0 OnGPIO 8 true; start";

fn boot_with_afib_episodes(episodes: usize) -> Option<Renode> {
    for (what, path) in [
        ("Renode", renode_binary()),
        ("the flash dump", repository().join("renode-sim/external_flash.bin")),
    ] {
        if !path.exists() {
            println!("skipping: {what} is not at {}", path.display());
            return None;
        }
    }
    let directory = scratch("stored-test");
    Rig::stock().generate(&directory, "rig");
    let renode = Renode::start(&directory, SCRIPT);
    renode.wait_for("shell>", BOOT_TIMEOUT).expect("the watch reaches its console");
    for episode in 1..=episodes {
        renode.shell("hwa10 fake_afib");
        renode
            .wait_for_count(EPISODE_STORED, episode, STORE_TIMEOUT)
            .expect("the watch stores the episode");
    }
    // The console runs on the main task and holds it: shell_init 0x59f54
    // tail-calls the line loop, so the boot that registers the WPP service
    // only carries on once the console is left.
    renode.shell("exit");
    renode
        .wait_for("Add WPPS chars.", BOOT_TIMEOUT)
        .expect("the watch finishes booting after the console");
    Some(renode)
}

fn episodes(output: &str) -> Vec<&str> {
    served(output, "ppg_afib")
}

fn served<'a>(output: &'a str, kind: &str) -> Vec<&'a str> {
    let prefix = format!("stored {kind} ");
    output.lines().filter(|line| line.starts_with(&prefix)).collect()
}

fn measured_at(episode: &str) -> i64 {
    episode
        .split_whitespace()
        .find_map(|field| field.strip_prefix("measured_at="))
        .expect("an episode line carries its time")
        .parse()
        .expect("the time is a number")
}

/// The wall clock the watch restores at boot, `[TIME] <now>/<rtc> (diff=..)`.
/// An episode `fake_afib` stores is stamped with the clock at that moment, so
/// the injected ones are those at or after it, and the dump's own are before.
fn boot_clock(log: &str) -> i64 {
    log.lines()
        .find_map(|line| line.split("[TIME] ").nth(1)?.split('/').next()?.parse().ok())
        .expect("the watch logs the clock it restored")
}

#[test]
fn measurements_the_watch_kept_reach_the_phone_once() {
    let Some(renode) = boot_with_afib_episodes(2) else {
        return;
    };

    let (synced, output) = client(&renode.directory, &["--sync"]);
    assert!(synced, "the sync did not finish:\n{}", tail(&output, 40));
    let kept = episodes(&output);
    let log = renode.log();
    let booted = boot_clock(&log);
    let injected = kept.iter().filter(|e| measured_at(e) >= booted).collect::<Vec<_>>();
    assert_eq!(injected.len(), 2, "both injected episodes are handed over:\n{}", tail(&output, 60));
    for episode in &injected {
        assert!(episode.contains("(139, 1)"), "the verdict is AFib: {episode}");
    }
    assert!(
        !log.contains("[PPG_AFIB] Signal deletion requested"),
        "a delete named the wrong episode:\n{}",
        tail(&log, 30)
    );

    println!("spot checks the dump held: {}", served(&output, "spo2_check").len());

    let (synced, output) = client(&renode.directory, &["--sync"]);
    assert!(synced, "the second sync did not finish:\n{}", tail(&output, 40));
    for kind in ["ppg_afib", "spo2_check", "ecg"] {
        assert!(
            served(&output, kind).is_empty(),
            "a {kind} was kept and still served again:\n{}",
            tail(&output, 40)
        );
    }
}
