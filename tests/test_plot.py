import numpy as np

from schem2spice import plot


def _csv(path, gain_db):
    f = np.logspace(0, 6, 241)
    rows = ["freq_hz,L_db,L_deg"] + [f"{a:.6g},{gain_db(a):.4f},0" for a in f]
    path.write_text("\n".join(rows) + "\n")


def test_render_draws_relative_and_riaa_curves(tmp_path):
    _csv(tmp_path / "flat_response.csv", lambda f: 40.0)
    _csv(tmp_path / "eq_response.csv", lambda f: 40.0 + float(plot.riaa_db(np.array([f]))[0]))
    system = {"outputs": {"L": "x"}, "plots": [
        {"name": "err", "x": [20, 20000], "curves": [
            {"case": "eq", "minus": "flat", "minus_ideal": "riaa", "normalize": 1000, "label": "error"}],
         "bands": [{"x": [20, 20000], "y": [-0.3, 0.3]}], "markers": [{"f": 1000, "db": 0}]}]}
    written = plot.render(system, tmp_path, "demo")
    assert [p.name for p in written] == ["err.svg", "demo_plots.html"]
    f, y = plot.curve(system["plots"][0]["curves"][0], {k: plot.load_response(tmp_path / f"{k}_response.csv")
                                                       for k in ("eq", "flat")}, "L")
    assert np.abs(y).max() < 1e-3  # an exact RIAA stage has no error
    assert "<polyline" in (tmp_path / "err.svg").read_text()
