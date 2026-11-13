"""The watcher's CPU figures: the share since the last poll of an instance, and - because each poll usually reaches a
different instance - the average since the process started, from one poll."""
import csv

import pytest

import watch_all as wa


class Clock:
    def __init__(self, monkeypatch, start=1000.0):
        self.now = start
        monkeypatch.setattr(wa.time, "time", lambda: self.now)


def health(instance="i-1", pid=1, uptime=60, cpu_seconds=30.0, **process):
    body = {"status": "healthy", "ontology_version": "1.10.0",
            "process": {"instance": instance, "pid": pid, "uptime_seconds": uptime, "rss_mb": 800, **process}}
    if cpu_seconds is not None:
        body["process"]["cpu_seconds"] = cpu_seconds
    return body


def poll(monkeypatch, watch, body):
    monkeypatch.setattr(wa, "fetch", lambda url, header, key, timeout: (200, 120.0, body))
    line, row, _ = watch.poll(5)
    return line, row


@pytest.fixture
def watch():
    return wa.AppWatch("ontology", "http://example/api/health", "x-functions-key", "k")


def test_an_instance_seen_once_shows_its_average_since_start(monkeypatch, watch):
    Clock(monkeypatch)
    line, row = poll(monkeypatch, watch, health(uptime=60, cpu_seconds=30.0))
    assert "cpu~  50%" in line and "CPU HIGH" not in line
    assert row["cpu_pct"] == "" and row["cpu_life_pct"] == 50.0
    assert watch.instances["i-1"]["life"] == 50.0


def test_a_second_poll_of_the_same_instance_shows_the_between_poll_figure(monkeypatch, watch):
    clock = Clock(monkeypatch)
    poll(monkeypatch, watch, health(uptime=60, cpu_seconds=30.0))
    clock.now += 5
    line, row = poll(monkeypatch, watch, health(uptime=65, cpu_seconds=34.0))
    assert "cpu   80%" in line and "~" not in line.split("hc")[0]
    assert row["cpu_pct"] == 80.0 and row["cpu_life_pct"] == pytest.approx(52.3, abs=0.05)


def test_a_high_average_on_an_instance_old_enough_is_flagged(monkeypatch, watch):
    Clock(monkeypatch)
    line, _ = poll(monkeypatch, watch, health(uptime=100, cpu_seconds=95.0))
    assert "cpu~  95%" in line and "CPU HIGH (avg)" in line


def test_a_young_instance_is_not_flagged_because_start_up_is_busy(monkeypatch, watch):
    Clock(monkeypatch)
    line, _ = poll(monkeypatch, watch, health(uptime=12, cpu_seconds=11.5))
    assert "cpu~  96%" in line and "CPU HIGH" not in line
    line, _ = poll(monkeypatch, watch, health(instance="i-2", uptime=wa.MIN_UPTIME_FLAG, cpu_seconds=29.0))
    assert "CPU HIGH (avg)" in line                                           # old enough from this age on


def test_a_measured_between_poll_figure_is_flagged_as_before_and_never_as_an_average(monkeypatch, watch):
    clock = Clock(monkeypatch)
    poll(monkeypatch, watch, health(uptime=100, cpu_seconds=10.0))
    clock.now += 5
    line, _ = poll(monkeypatch, watch, health(uptime=105, cpu_seconds=14.8))
    assert "cpu   96%" in line and "CPU HIGH" in line and "(avg)" not in line


def test_the_cpu_limit_is_what_the_average_is_judged_against(monkeypatch, watch):
    Clock(monkeypatch)
    line, row = poll(monkeypatch, watch, health(uptime=100, cpu_seconds=150.0, cpu_limit_cores=2))
    assert "cpu~ 150%" in line and "CPU HIGH" not in line                       # 150% of one core is 75% of two cores
    line, _ = poll(monkeypatch, watch, health(instance="i-2", uptime=100, cpu_seconds=95.0, cpu_limit_cores=1))
    assert "CPU HIGH (avg)" in line


def test_an_average_needs_a_little_uptime_and_an_app_that_reports_cpu(monkeypatch, watch):
    Clock(monkeypatch)
    line, row = poll(monkeypatch, watch, health(uptime=1, cpu_seconds=0.9))
    assert "cpu    -" in line and row["cpu_life_pct"] == "" and watch.instances["i-1"]["life"] is None
    line, row = poll(monkeypatch, watch, health(instance="i-2", uptime=300, cpu_seconds=None))      # an app without the CPU health patch
    assert "cpu    -" in line and row["cpu_life_pct"] == ""


def _watch_with(monkeypatch, watch, readings):
    """readings: [(uptime, cpu_seconds)], each a different instance."""
    Clock(monkeypatch)
    for i, (uptime, cpu) in enumerate(readings):
        poll(monkeypatch, watch, health(instance=f"i-{i}", pid=i, uptime=uptime, cpu_seconds=cpu))
    return watch


def test_the_summary_shows_how_the_instances_share_the_cpu(monkeypatch, watch):
    _watch_with(monkeypatch, watch, [(100, 10.0), (100, 40.0), (100, 80.0), (100, 95.0), (10, 9.9)])
    text = wa.cpu_rollup(watch)
    values = [10.0, 40.0, 80.0, 95.0]
    assert "4 of 5 instances" in text and "max 95%" in text and "2 at or above 70%" in text
    assert f"median {wa.percentile(values, 50):.0f}%" in text and f"p90 {wa.percentile(values, 90):.0f}%" in text


def test_the_summary_says_nothing_when_no_instance_is_old_enough(monkeypatch, watch):
    _watch_with(monkeypatch, watch, [(5, 4.0), (15, 14.0)])
    assert wa.cpu_rollup(watch) is None


def test_the_summary_judges_against_the_cpu_limit(monkeypatch, watch):
    Clock(monkeypatch)
    poll(monkeypatch, watch, health(uptime=100, cpu_seconds=120.0, cpu_limit_cores=2))   # 120% of one core is 60% of two
    text = wa.cpu_rollup(watch)
    assert "0 at or above 70%" in text and "max 60%" in text
    poll(monkeypatch, watch, health(instance="i-2", pid=2, uptime=100, cpu_seconds=150.0, cpu_limit_cores=2))   # 75%: busy
    assert "1 at or above 70%" in wa.cpu_rollup(watch)


def test_an_instances_summary_line(monkeypatch, watch):
    clock = Clock(monkeypatch)
    poll(monkeypatch, watch, health(uptime=60, cpu_seconds=30.0))
    assert wa.summary_cpu_text(watch.instances["i-1"]) == "cpu ~50% since start"
    clock.now += 5
    poll(monkeypatch, watch, health(uptime=65, cpu_seconds=34.0))
    assert wa.summary_cpu_text(watch.instances["i-1"]) == "cpu avg 80% max 80% (~52% since start)"
    assert wa.summary_cpu_text({"cpu": [], "life": None}) == "cpu -"
    assert wa.summary_cpu_text({"cpu": [40.0, 60.0], "life": None}) == "cpu avg 50% max 60%"


def test_the_csv_has_the_new_column_and_an_older_file_keeps_its_own_columns(tmp_path, monkeypatch, watch):
    Clock(monkeypatch)
    assert wa.csv_fieldnames(str(tmp_path / "missing.csv")) == wa.CSV_FIELDS
    old = tmp_path / "old.csv"
    old_columns = wa.CSV_FIELDS[:-1]
    old.write_text(",".join(old_columns) + "\n", encoding="utf-8")
    assert wa.csv_fieldnames(str(old)) == old_columns
    _, row = poll(monkeypatch, watch, health(uptime=60, cpu_seconds=30.0))
    with open(old, "a", newline="", encoding="utf-8") as handle:                  # the way main() appends to it
        csv.DictWriter(handle, fieldnames=old_columns, extrasaction="ignore").writerow(row)
    rows = list(csv.DictReader(open(old, encoding="utf-8")))
    assert rows[0]["cpu_seconds"] == "30.0" and "cpu_life_pct" not in rows[0]
    fresh = tmp_path / "fresh.csv"
    with open(fresh, "w", newline="", encoding="utf-8") as handle:
        writer = csv.DictWriter(handle, fieldnames=wa.CSV_FIELDS, extrasaction="ignore")
        writer.writeheader()
        writer.writerow(row)
    assert list(csv.DictReader(open(fresh, encoding="utf-8")))[0]["cpu_life_pct"] == "50.0"
