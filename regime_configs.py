"""Per-coin per-regime configs (auto-generated from regime tuner).
Generated from 90 days of HL 4h regime data.

Regimes: bull-calm, bull-storm, bear-calm, bear-storm, chop
Each entry maps coin -> regime -> {RH, RL, sigs, flt, TP, SL, n, wr, pnl}.

# Coverage stats from tuning:
# bull-calm: 139/139 coins
# bull-storm: 139/139 coins
# bear-calm: 42/139 coins
# bear-storm: 130/139 coins
# chop: 139/139 coins
"""

REGIME_CONFIGS = {
  "ALT": {
    "bull-calm": {
      "RH": 70,
      "RL": 22,
      "sigs": [
        "BB"
      ],
      "flt": "none",
      "TP": 0.02,
      "SL": 0.05,
      "n": 7,
      "wr": 0.857,
      "pnl": 0.1009,
      "score": 8.653
    },
    "bull-storm": {
      "RH": 70,
      "RL": 30,
      "sigs": [
        "PV",
        "BB"
      ],
      "flt": "none",
      "TP": 0.02,
      "SL": 0.05,
      "n": 21,
      "wr": 0.81,
      "pnl": 0.204,
      "score": 16.518
    },
    "bear-calm": {
      "reason": "no_valid_combo"
    },
    "bear-storm": {
      "RH": 78,
      "RL": 30,
      "sigs": [
        "PV"
      ],
      "flt": "none",
      "TP": 0.02,
      "SL": 0.05,
      "n": 5,
      "wr": 1.0,
      "pnl": 0.0955,
      "score": 9.55
    },
    "chop": {
      "RH": 78,
      "RL": 30,
      "sigs": [
        "PV"
      ],
      "flt": "none",
      "TP": 0.02,
      "SL": 0.05,
      "n": 10,
      "wr": 0.8,
      "pnl": 0.0893,
      "score": 7.14
    }
  },
  "ASTER": {
    "bull-calm": {
      "RH": 70,
      "RL": 22,
      "sigs": [
        "PV",
        "BB"
      ],
      "flt": "none",
      "TP": 0.02,
      "SL": 0.05,
      "n": 22,
      "wr": 0.818,
      "pnl": 0.1906,
      "score": 15.595
    },
    "bull-storm": {
      "RH": 70,
      "RL": 22,
      "sigs": [
        "PV"
      ],
      "flt": "none",
      "TP": 0.02,
      "SL": 0.05,
      "n": 10,
      "wr": 0.7,
      "pnl": 0.0801,
      "score": 5.605
    },
    "bear-calm": {
      "reason": "no_valid_combo"
    },
    "bear-storm": {
      "RH": 78,
      "RL": 30,
      "sigs": [
        "PV"
      ],
      "flt": "none",
      "TP": 0.02,
      "SL": 0.05,
      "n": 5,
      "wr": 0.8,
      "pnl": 0.0255,
      "score": 2.04
    },
    "chop": {
      "RH": 78,
      "RL": 30,
      "sigs": [
        "PV",
        "BB"
      ],
      "flt": "none",
      "TP": 0.02,
      "SL": 0.05,
      "n": 22,
      "wr": 0.955,
      "pnl": 0.3443,
      "score": 32.867
    }
  },
  "BERA": {
    "bull-calm": {
      "RH": 78,
      "RL": 22,
      "sigs": [
        "BB"
      ],
      "flt": "none",
      "TP": 0.006,
      "SL": 0.05,
      "n": 7,
      "wr": 0.857,
      "pnl": 0.0069,
      "score": 0.592
    },
    "bull-storm": {
      "RH": 70,
      "RL": 30,
      "sigs": [
        "PV",
        "BB"
      ],
      "flt": "none",
      "TP": 0.012,
      "SL": 0.05,
      "n": 13,
      "wr": 0.923,
      "pnl": 0.1143,
      "score": 10.555
    },
    "bear-calm": {
      "reason": "no_valid_combo"
    },
    "bear-storm": {
      "reason": "no_valid_combo"
    },
    "chop": {
      "RH": 78,
      "RL": 30,
      "sigs": [
        "PV"
      ],
      "flt": "none",
      "TP": 0.02,
      "SL": 0.05,
      "n": 15,
      "wr": 0.933,
      "pnl": 0.2032,
      "score": 18.968
    }
  },
  "FET": {
    "bull-calm": {
      "RH": 70,
      "RL": 22,
      "sigs": [
        "PV",
        "BB"
      ],
      "flt": "none",
      "TP": 0.006,
      "SL": 0.05,
      "n": 11,
      "wr": 0.909,
      "pnl": 0.0278,
      "score": 2.53
    },
    "bull-storm": {
      "RH": 70,
      "RL": 30,
      "sigs": [
        "PV",
        "BB"
      ],
      "flt": "none",
      "TP": 0.02,
      "SL": 0.05,
      "n": 16,
      "wr": 0.875,
      "pnl": 0.2029,
      "score": 17.751
    },
    "bear-calm": {
      "reason": "no_valid_combo"
    },
    "bear-storm": {
      "RH": 70,
      "RL": 30,
      "sigs": [
        "PV",
        "BB"
      ],
      "flt": "none",
      "TP": 0.02,
      "SL": 0.05,
      "n": 7,
      "wr": 1.0,
      "pnl": 0.1337,
      "score": 13.37
    },
    "chop": {
      "RH": 78,
      "RL": 30,
      "sigs": [
        "PV",
        "BB"
      ],
      "flt": "none",
      "TP": 0.02,
      "SL": 0.05,
      "n": 23,
      "wr": 0.913,
      "pnl": 0.2993,
      "score": 27.327
    }
  },
  "IMX": {
    "bull-calm": {
      "RH": 70,
      "RL": 22,
      "sigs": [
        "PV",
        "BB"
      ],
      "flt": "none",
      "TP": 0.012,
      "SL": 0.05,
      "n": 11,
      "wr": 0.909,
      "pnl": 0.0918,
      "score": 8.343
    },
    "bull-storm": {
      "RH": 78,
      "RL": 30,
      "sigs": [
        "PV",
        "BB"
      ],
      "flt": "none",
      "TP": 0.012,
      "SL": 0.05,
      "n": 12,
      "wr": 0.917,
      "pnl": 0.0712,
      "score": 6.527
    },
    "bear-calm": {
      "reason": "no_valid_combo"
    },
    "bear-storm": {
      "RH": 70,
      "RL": 30,
      "sigs": [
        "PV"
      ],
      "flt": "none",
      "TP": 0.006,
      "SL": 0.05,
      "n": 8,
      "wr": 1.0,
      "pnl": 0.0408,
      "score": 4.08
    },
    "chop": {
      "RH": 78,
      "RL": 22,
      "sigs": [
        "BB"
      ],
      "flt": "none",
      "TP": 0.02,
      "SL": 0.05,
      "n": 6,
      "wr": 0.333,
      "pnl": -0.1654,
      "score": -5.513
    }
  },
  "PROMPT": {
    "bull-calm": {
      "RH": 70,
      "RL": 22,
      "sigs": [
        "BB"
      ],
      "flt": "none",
      "TP": 0.006,
      "SL": 0.05,
      "n": 8,
      "wr": 1.0,
      "pnl": 0.0408,
      "score": 4.08
    },
    "bull-storm": {
      "RH": 70,
      "RL": 22,
      "sigs": [
        "PV",
        "BB"
      ],
      "flt": "none",
      "TP": 0.02,
      "SL": 0.05,
      "n": 11,
      "wr": 0.909,
      "pnl": 0.1677,
      "score": 15.248
    },
    "bear-calm": {
      "reason": "no_valid_combo"
    },
    "bear-storm": {
      "reason": "no_valid_combo"
    },
    "chop": {
      "RH": 78,
      "RL": 30,
      "sigs": [
        "BB"
      ],
      "flt": "none",
      "TP": 0.02,
      "SL": 0.05,
      "n": 6,
      "wr": 1.0,
      "pnl": 0.1092,
      "score": 10.92
    }
  },
  "RENDER": {
    "bull-calm": {
      "RH": 70,
      "RL": 22,
      "sigs": [
        "PV",
        "BB"
      ],
      "flt": "none",
      "TP": 0.02,
      "SL": 0.05,
      "n": 12,
      "wr": 0.917,
      "pnl": 0.1592,
      "score": 14.593
    },
    "bull-storm": {
      "RH": 70,
      "RL": 30,
      "sigs": [
        "PV",
        "BB"
      ],
      "flt": "none",
      "TP": 0.02,
      "SL": 0.05,
      "n": 18,
      "wr": 0.778,
      "pnl": 0.1337,
      "score": 10.4
    },
    "bear-calm": {
      "reason": "no_valid_combo"
    },
    "bear-storm": {
      "RH": 70,
      "RL": 30,
      "sigs": [
        "PV"
      ],
      "flt": "none",
      "TP": 0.02,
      "SL": 0.05,
      "n": 8,
      "wr": 1.0,
      "pnl": 0.1528,
      "score": 15.28
    },
    "chop": {
      "RH": 78,
      "RL": 30,
      "sigs": [
        "BB"
      ],
      "flt": "none",
      "TP": 0.02,
      "SL": 0.05,
      "n": 11,
      "wr": 0.909,
      "pnl": 0.1401,
      "score": 12.736
    }
  },
  "STRK": {
    "bull-calm": {
      "RH": 70,
      "RL": 30,
      "sigs": [
        "PV",
        "BB"
      ],
      "flt": "none",
      "TP": 0.02,
      "SL": 0.05,
      "n": 29,
      "wr": 0.828,
      "pnl": 0.2969,
      "score": 24.569
    },
    "bull-storm": {
      "RH": 70,
      "RL": 30,
      "sigs": [
        "PV",
        "BB"
      ],
      "flt": "none",
      "TP": 0.02,
      "SL": 0.05,
      "n": 17,
      "wr": 0.882,
      "pnl": 0.2216,
      "score": 19.553
    },
    "bear-calm": {
      "reason": "no_valid_combo"
    },
    "bear-storm": {
      "RH": 70,
      "RL": 30,
      "sigs": [
        "PV",
        "BB"
      ],
      "flt": "none",
      "TP": 0.02,
      "SL": 0.05,
      "n": 8,
      "wr": 1.0,
      "pnl": 0.1528,
      "score": 15.28
    },
    "chop": {
      "RH": 78,
      "RL": 30,
      "sigs": [
        "PV",
        "BB"
      ],
      "flt": "none",
      "TP": 0.02,
      "SL": 0.05,
      "n": 22,
      "wr": 0.909,
      "pnl": 0.2802,
      "score": 25.473
    }
  },
  "SUPER": {
    "bull-calm": {
      "RH": 70,
      "RL": 22,
      "sigs": [
        "BB"
      ],
      "flt": "none",
      "TP": 0.006,
      "SL": 0.05,
      "n": 5,
      "wr": 0.8,
      "pnl": -0.0305,
      "score": -2.44
    },
    "bull-storm": {
      "RH": 70,
      "RL": 22,
      "sigs": [
        "PV",
        "BB"
      ],
      "flt": "none",
      "TP": 0.02,
      "SL": 0.05,
      "n": 8,
      "wr": 1.0,
      "pnl": 0.1528,
      "score": 15.28
    },
    "bear-calm": {
      "reason": "no_valid_combo"
    },
    "bear-storm": {
      "RH": 78,
      "RL": 30,
      "sigs": [
        "PV",
        "BB"
      ],
      "flt": "none",
      "TP": 0.02,
      "SL": 0.05,
      "n": 6,
      "wr": 1.0,
      "pnl": 0.1146,
      "score": 11.46
    },
    "chop": {
      "RH": 78,
      "RL": 30,
      "sigs": [
        "BB"
      ],
      "flt": "none",
      "TP": 0.02,
      "SL": 0.05,
      "n": 10,
      "wr": 0.8,
      "pnl": 0.075,
      "score": 5.998
    }
  },
  "W": {
    "bull-calm": {
      "RH": 70,
      "RL": 30,
      "sigs": [
        "BB"
      ],
      "flt": "none",
      "TP": 0.012,
      "SL": 0.05,
      "n": 13,
      "wr": 1.0,
      "pnl": 0.1443,
      "score": 14.43
    },
    "bull-storm": {
      "RH": 70,
      "RL": 22,
      "sigs": [
        "BB"
      ],
      "flt": "none",
      "TP": 0.012,
      "SL": 0.05,
      "n": 7,
      "wr": 1.0,
      "pnl": 0.0777,
      "score": 7.77
    },
    "bear-calm": {
      "reason": "no_valid_combo"
    },
    "bear-storm": {
      "RH": 70,
      "RL": 30,
      "sigs": [
        "PV"
      ],
      "flt": "none",
      "TP": 0.02,
      "SL": 0.05,
      "n": 8,
      "wr": 1.0,
      "pnl": 0.1528,
      "score": 15.28
    },
    "chop": {
      "RH": 78,
      "RL": 30,
      "sigs": [
        "PV"
      ],
      "flt": "none",
      "TP": 0.02,
      "SL": 0.05,
      "n": 20,
      "wr": 0.8,
      "pnl": 0.102,
      "score": 8.16
    }
  },
  "WCT": {
    "bull-calm": {
      "RH": 78,
      "RL": 30,
      "sigs": [
        "PV",
        "BB"
      ],
      "flt": "none",
      "TP": 0.02,
      "SL": 0.05,
      "n": 19,
      "wr": 0.789,
      "pnl": 0.0639,
      "score": 5.045
    },
    "bull-storm": {
      "RH": 70,
      "RL": 22,
      "sigs": [
        "PV",
        "BB"
      ],
      "flt": "none",
      "TP": 0.02,
      "SL": 0.05,
      "n": 14,
      "wr": 0.857,
      "pnl": 0.1274,
      "score": 10.92
    },
    "bear-calm": {
      "reason": "no_valid_combo"
    },
    "bear-storm": {
      "RH": 78,
      "RL": 30,
      "sigs": [
        "PV",
        "BB"
      ],
      "flt": "none",
      "TP": 0.006,
      "SL": 0.05,
      "n": 6,
      "wr": 0.833,
      "pnl": -0.0254,
      "score": -2.117
    },
    "chop": {
      "RH": 78,
      "RL": 30,
      "sigs": [
        "PV",
        "BB"
      ],
      "flt": "none",
      "TP": 0.02,
      "SL": 0.05,
      "n": 21,
      "wr": 0.714,
      "pnl": 0.039,
      "score": 2.788
    }
  },
  "WLFI": {
    "bull-calm": {
      "RH": 70,
      "RL": 22,
      "sigs": [
        "PV"
      ],
      "flt": "none",
      "TP": 0.02,
      "SL": 0.05,
      "n": 7,
      "wr": 1.0,
      "pnl": 0.1337,
      "score": 13.37
    },
    "bull-storm": {
      "RH": 70,
      "RL": 22,
      "sigs": [
        "PV"
      ],
      "flt": "none",
      "TP": 0.02,
      "SL": 0.05,
      "n": 12,
      "wr": 0.75,
      "pnl": 0.0396,
      "score": 2.971
    },
    "bear-calm": {
      "reason": "no_valid_combo"
    },
    "bear-storm": {
      "RH": 70,
      "RL": 30,
      "sigs": [
        "PV",
        "BB"
      ],
      "flt": "none",
      "TP": 0.012,
      "SL": 0.05,
      "n": 8,
      "wr": 1.0,
      "pnl": 0.0888,
      "score": 8.88
    },
    "chop": {
      "RH": 70,
      "RL": 30,
      "sigs": [
        "PV",
        "BB"
      ],
      "flt": "none",
      "TP": 0.02,
      "SL": 0.05,
      "n": 37,
      "wr": 0.838,
      "pnl": 0.2601,
      "score": 21.793
    }
  },
  "XAI": {
    "bull-calm": {
      "RH": 78,
      "RL": 30,
      "sigs": [
        "PV",
        "BB"
      ],
      "flt": "none",
      "TP": 0.02,
      "SL": 0.05,
      "n": 15,
      "wr": 0.933,
      "pnl": 0.2165,
      "score": 20.207
    },
    "bull-storm": {
      "RH": 70,
      "RL": 30,
      "sigs": [
        "PV"
      ],
      "flt": "none",
      "TP": 0.02,
      "SL": 0.05,
      "n": 14,
      "wr": 1.0,
      "pnl": 0.2674,
      "score": 26.74
    },
    "bear-calm": {
      "reason": "no_valid_combo"
    },
    "bear-storm": {
      "RH": 70,
      "RL": 22,
      "sigs": [
        "PV"
      ],
      "flt": "none",
      "TP": 0.006,
      "SL": 0.05,
      "n": 8,
      "wr": 0.875,
      "pnl": 0.0121,
      "score": 1.059
    },
    "chop": {
      "RH": 78,
      "RL": 30,
      "sigs": [
        "PV"
      ],
      "flt": "none",
      "TP": 0.012,
      "SL": 0.05,
      "n": 15,
      "wr": 0.867,
      "pnl": 0.0425,
      "score": 3.683
    }
  },
  "ZK": {
    "bull-calm": {
      "RH": 70,
      "RL": 22,
      "sigs": [
        "PV",
        "BB"
      ],
      "flt": "none",
      "TP": 0.02,
      "SL": 0.05,
      "n": 15,
      "wr": 0.8,
      "pnl": 0.1111,
      "score": 8.889
    },
    "bull-storm": {
      "RH": 78,
      "RL": 30,
      "sigs": [
        "BB"
      ],
      "flt": "none",
      "TP": 0.02,
      "SL": 0.05,
      "n": 9,
      "wr": 0.778,
      "pnl": 0.0984,
      "score": 7.654
    },
    "bear-calm": {
      "reason": "no_valid_combo"
    },
    "bear-storm": {
      "RH": 70,
      "RL": 30,
      "sigs": [
        "PV",
        "BB"
      ],
      "flt": "none",
      "TP": 0.02,
      "SL": 0.05,
      "n": 9,
      "wr": 1.0,
      "pnl": 0.1719,
      "score": 17.19
    },
    "chop": {
      "RH": 78,
      "RL": 30,
      "sigs": [
        "PV",
        "BB"
      ],
      "flt": "none",
      "TP": 0.02,
      "SL": 0.05,
      "n": 26,
      "wr": 0.846,
      "pnl": 0.2698,
      "score": 22.83
    }
  },
  "AAVE": {
    "bull-calm": {
      "RH": 78,
      "RL": 30,
      "sigs": [
        "BB"
      ],
      "flt": "none",
      "TP": 0.006,
      "SL": 0.05,
      "n": 6,
      "wr": 1.0,
      "pnl": 0.0306,
      "score": 3.06
    },
    "bull-storm": {
      "RH": 70,
      "RL": 30,
      "sigs": [
        "PV",
        "BB"
      ],
      "flt": "none",
      "TP": 0.02,
      "SL": 0.05,
      "n": 19,
      "wr": 0.842,
      "pnl": 0.1558,
      "score": 13.123
    },
    "bear-calm": {
      "RH": 70,
      "RL": 22,
      "sigs": [
        "PV",
        "BB"
      ],
      "flt": "none",
      "TP": 0.02,
      "SL": 0.05,
      "n": 9,
      "wr": 0.889,
      "pnl": 0.0893,
      "score": 7.934
    },
    "bear-storm": {
      "RH": 70,
      "RL": 30,
      "sigs": [
        "PV"
      ],
      "flt": "none",
      "TP": 0.006,
      "SL": 0.05,
      "n": 9,
      "wr": 0.778,
      "pnl": -0.0661,
      "score": -5.141
    },
    "chop": {
      "RH": 78,
      "RL": 30,
      "sigs": [
        "PV",
        "BB"
      ],
      "flt": "none",
      "TP": 0.02,
      "SL": 0.05,
      "n": 27,
      "wr": 0.778,
      "pnl": 0.0957,
      "score": 7.443
    }
  },
  "ADA": {
    "bull-calm": {
      "RH": 70,
      "RL": 22,
      "sigs": [
        "PV"
      ],
      "flt": "none",
      "TP": 0.012,
      "SL": 0.05,
      "n": 12,
      "wr": 0.917,
      "pnl": 0.0712,
      "score": 6.527
    },
    "bull-storm": {
      "RH": 70,
      "RL": 30,
      "sigs": [
        "PV",
        "BB"
      ],
      "flt": "none",
      "TP": 0.02,
      "SL": 0.05,
      "n": 22,
      "wr": 0.727,
      "pnl": 0.0797,
      "score": 5.795
    },
    "bear-calm": {
      "RH": 70,
      "RL": 30,
      "sigs": [
        "PV"
      ],
      "flt": "none",
      "TP": 0.02,
      "SL": 0.05,
      "n": 7,
      "wr": 0.857,
      "pnl": 0.0953,
      "score": 8.168
    },
    "bear-storm": {
      "RH": 70,
      "RL": 30,
      "sigs": [
        "PV",
        "BB"
      ],
      "flt": "none",
      "TP": 0.006,
      "SL": 0.05,
      "n": 6,
      "wr": 1.0,
      "pnl": 0.0306,
      "score": 3.06
    },
    "chop": {
      "RH": 78,
      "RL": 30,
      "sigs": [
        "BB"
      ],
      "flt": "none",
      "TP": 0.02,
      "SL": 0.05,
      "n": 19,
      "wr": 0.947,
      "pnl": 0.3222,
      "score": 30.524
    }
  },
  "AERO": {
    "bull-calm": {
      "RH": 70,
      "RL": 22,
      "sigs": [
        "PV",
        "BB"
      ],
      "flt": "none",
      "TP": 0.02,
      "SL": 0.05,
      "n": 10,
      "wr": 0.9,
      "pnl": 0.121,
      "score": 10.89
    },
    "bull-storm": {
      "RH": 70,
      "RL": 22,
      "sigs": [
        "PV",
        "BB"
      ],
      "flt": "none",
      "TP": 0.006,
      "SL": 0.05,
      "n": 11,
      "wr": 1.0,
      "pnl": 0.0561,
      "score": 5.61
    },
    "bear-calm": {
      "RH": 70,
      "RL": 30,
      "sigs": [
        "PV",
        "BB"
      ],
      "flt": "none",
      "TP": 0.02,
      "SL": 0.05,
      "n": 26,
      "wr": 0.808,
      "pnl": 0.1562,
      "score": 12.613
    },
    "bear-storm": {
      "RH": 78,
      "RL": 30,
      "sigs": [
        "PV"
      ],
      "flt": "none",
      "TP": 0.02,
      "SL": 0.05,
      "n": 5,
      "wr": 1.0,
      "pnl": 0.0955,
      "score": 9.55
    },
    "chop": {
      "RH": 78,
      "RL": 30,
      "sigs": [
        "PV",
        "BB"
      ],
      "flt": "none",
      "TP": 0.02,
      "SL": 0.05,
      "n": 33,
      "wr": 0.818,
      "pnl": 0.2018,
      "score": 16.507
    }
  },
  "AR": {
    "bull-calm": {
      "RH": 70,
      "RL": 30,
      "sigs": [
        "PV",
        "BB"
      ],
      "flt": "none",
      "TP": 0.02,
      "SL": 0.05,
      "n": 34,
      "wr": 0.824,
      "pnl": 0.2294,
      "score": 18.892
    },
    "bull-storm": {
      "RH": 70,
      "RL": 22,
      "sigs": [
        "PV"
      ],
      "flt": "none",
      "TP": 0.02,
      "SL": 0.05,
      "n": 6,
      "wr": 1.0,
      "pnl": 0.1146,
      "score": 11.46
    },
    "bear-calm": {
      "RH": 70,
      "RL": 30,
      "sigs": [
        "PV",
        "BB"
      ],
      "flt": "none",
      "TP": 0.02,
      "SL": 0.05,
      "n": 14,
      "wr": 0.929,
      "pnl": 0.211,
      "score": 19.597
    },
    "bear-storm": {
      "RH": 78,
      "RL": 30,
      "sigs": [
        "PV",
        "BB"
      ],
      "flt": "none",
      "TP": 0.012,
      "SL": 0.05,
      "n": 5,
      "wr": 1.0,
      "pnl": 0.0555,
      "score": 5.55
    },
    "chop": {
      "RH": 78,
      "RL": 30,
      "sigs": [
        "BB"
      ],
      "flt": "none",
      "TP": 0.02,
      "SL": 0.05,
      "n": 13,
      "wr": 0.923,
      "pnl": 0.16,
      "score": 14.767
    }
  },
  "ATOM": {
    "bull-calm": {
      "RH": 70,
      "RL": 30,
      "sigs": [
        "BB"
      ],
      "flt": "none",
      "TP": 0.02,
      "SL": 0.05,
      "n": 16,
      "wr": 0.75,
      "pnl": 0.1286,
      "score": 9.644
    },
    "bull-storm": {
      "RH": 78,
      "RL": 22,
      "sigs": [
        "PV",
        "BB"
      ],
      "flt": "none",
      "TP": 0.02,
      "SL": 0.05,
      "n": 5,
      "wr": 0.8,
      "pnl": 0.0589,
      "score": 4.713
    },
    "bear-calm": {
      "RH": 70,
      "RL": 30,
      "sigs": [
        "PV",
        "BB"
      ],
      "flt": "none",
      "TP": 0.012,
      "SL": 0.05,
      "n": 5,
      "wr": 1.0,
      "pnl": 0.0449,
      "score": 4.494
    },
    "bear-storm": {
      "RH": 70,
      "RL": 30,
      "sigs": [
        "PV",
        "BB"
      ],
      "flt": "none",
      "TP": 0.012,
      "SL": 0.05,
      "n": 8,
      "wr": 0.75,
      "pnl": 0.0464,
      "score": 3.481
    },
    "chop": {
      "RH": 78,
      "RL": 30,
      "sigs": [
        "PV",
        "BB"
      ],
      "flt": "none",
      "TP": 0.012,
      "SL": 0.05,
      "n": 19,
      "wr": 0.895,
      "pnl": 0.1204,
      "score": 10.771
    }
  },
  "CHILLGUY": {
    "bull-calm": {
      "RH": 70,
      "RL": 22,
      "sigs": [
        "PV"
      ],
      "flt": "none",
      "TP": 0.02,
      "SL": 0.05,
      "n": 16,
      "wr": 0.938,
      "pnl": 0.2356,
      "score": 22.087
    },
    "bull-storm": {
      "RH": 78,
      "RL": 30,
      "sigs": [
        "PV",
        "BB"
      ],
      "flt": "none",
      "TP": 0.02,
      "SL": 0.05,
      "n": 8,
      "wr": 1.0,
      "pnl": 0.1528,
      "score": 15.28
    },
    "bear-calm": {
      "reason": "no_valid_combo"
    },
    "bear-storm": {
      "RH": 70,
      "RL": 22,
      "sigs": [
        "PV"
      ],
      "flt": "none",
      "TP": 0.02,
      "SL": 0.05,
      "n": 6,
      "wr": 0.833,
      "pnl": 0.0446,
      "score": 3.717
    },
    "chop": {
      "RH": 70,
      "RL": 30,
      "sigs": [
        "PV",
        "BB"
      ],
      "flt": "ema200",
      "TP": 0.006,
      "SL": 0.05,
      "n": 5,
      "wr": 1.0,
      "pnl": 0.0255,
      "score": 2.55
    }
  },
  "COMP": {
    "bull-calm": {
      "RH": 78,
      "RL": 30,
      "sigs": [
        "BB"
      ],
      "flt": "none",
      "TP": 0.02,
      "SL": 0.05,
      "n": 12,
      "wr": 0.917,
      "pnl": 0.1592,
      "score": 14.593
    },
    "bull-storm": {
      "RH": 78,
      "RL": 30,
      "sigs": [
        "BB"
      ],
      "flt": "none",
      "TP": 0.02,
      "SL": 0.05,
      "n": 8,
      "wr": 1.0,
      "pnl": 0.1528,
      "score": 15.28
    },
    "bear-calm": {
      "RH": 70,
      "RL": 30,
      "sigs": [
        "BB"
      ],
      "flt": "none",
      "TP": 0.02,
      "SL": 0.05,
      "n": 7,
      "wr": 0.857,
      "pnl": 0.0877,
      "score": 7.518
    },
    "bear-storm": {
      "RH": 70,
      "RL": 30,
      "sigs": [
        "PV",
        "BB"
      ],
      "flt": "none",
      "TP": 0.006,
      "SL": 0.05,
      "n": 6,
      "wr": 1.0,
      "pnl": 0.0306,
      "score": 3.06
    },
    "chop": {
      "RH": 78,
      "RL": 30,
      "sigs": [
        "BB"
      ],
      "flt": "none",
      "TP": 0.012,
      "SL": 0.05,
      "n": 15,
      "wr": 0.8,
      "pnl": -0.0195,
      "score": -1.56
    }
  },
  "DOT": {
    "bull-calm": {
      "RH": 78,
      "RL": 22,
      "sigs": [
        "BB"
      ],
      "flt": "none",
      "TP": 0.012,
      "SL": 0.05,
      "n": 6,
      "wr": 0.833,
      "pnl": 0.0046,
      "score": 0.383
    },
    "bull-storm": {
      "RH": 70,
      "RL": 22,
      "sigs": [
        "PV"
      ],
      "flt": "none",
      "TP": 0.012,
      "SL": 0.05,
      "n": 6,
      "wr": 0.833,
      "pnl": 0.0046,
      "score": 0.383
    },
    "bear-calm": {
      "RH": 70,
      "RL": 30,
      "sigs": [
        "PV",
        "BB"
      ],
      "flt": "none",
      "TP": 0.02,
      "SL": 0.05,
      "n": 10,
      "wr": 0.8,
      "pnl": 0.1029,
      "score": 8.233
    },
    "bear-storm": {
      "RH": 70,
      "RL": 22,
      "sigs": [
        "PV",
        "BB"
      ],
      "flt": "none",
      "TP": 0.02,
      "SL": 0.05,
      "n": 5,
      "wr": 1.0,
      "pnl": 0.0955,
      "score": 9.55
    },
    "chop": {
      "RH": 78,
      "RL": 30,
      "sigs": [
        "PV",
        "BB"
      ],
      "flt": "none",
      "TP": 0.02,
      "SL": 0.05,
      "n": 22,
      "wr": 0.909,
      "pnl": 0.2802,
      "score": 25.473
    }
  },
  "DYDX": {
    "bull-calm": {
      "RH": 78,
      "RL": 22,
      "sigs": [
        "BB"
      ],
      "flt": "none",
      "TP": 0.012,
      "SL": 0.05,
      "n": 5,
      "wr": 0.6,
      "pnl": -0.0685,
      "score": -4.11
    },
    "bull-storm": {
      "RH": 70,
      "RL": 22,
      "sigs": [
        "PV"
      ],
      "flt": "none",
      "TP": 0.02,
      "SL": 0.05,
      "n": 9,
      "wr": 0.889,
      "pnl": 0.1497,
      "score": 13.305
    },
    "bear-calm": {
      "reason": "no_valid_combo"
    },
    "bear-storm": {
      "RH": 70,
      "RL": 30,
      "sigs": [
        "PV"
      ],
      "flt": "none",
      "TP": 0.02,
      "SL": 0.05,
      "n": 6,
      "wr": 0.667,
      "pnl": -0.0254,
      "score": -1.693
    },
    "chop": {
      "RH": 78,
      "RL": 30,
      "sigs": [
        "PV"
      ],
      "flt": "ema200",
      "TP": 0.02,
      "SL": 0.05,
      "n": 6,
      "wr": 1.0,
      "pnl": 0.1146,
      "score": 11.46
    }
  },
  "DYM": {
    "bull-calm": {
      "RH": 70,
      "RL": 22,
      "sigs": [
        "BB"
      ],
      "flt": "none",
      "TP": 0.012,
      "SL": 0.05,
      "n": 6,
      "wr": 0.667,
      "pnl": -0.0574,
      "score": -3.827
    },
    "bull-storm": {
      "RH": 70,
      "RL": 30,
      "sigs": [
        "BB"
      ],
      "flt": "none",
      "TP": 0.006,
      "SL": 0.05,
      "n": 13,
      "wr": 0.923,
      "pnl": 0.0103,
      "score": 0.951
    },
    "bear-calm": {
      "reason": "no_valid_combo"
    },
    "bear-storm": {
      "RH": 70,
      "RL": 30,
      "sigs": [
        "PV",
        "BB"
      ],
      "flt": "none",
      "TP": 0.02,
      "SL": 0.05,
      "n": 10,
      "wr": 0.8,
      "pnl": 0.051,
      "score": 4.08
    },
    "chop": {
      "RH": 78,
      "RL": 30,
      "sigs": [
        "PV",
        "BB"
      ],
      "flt": "ema200",
      "TP": 0.02,
      "SL": 0.05,
      "n": 7,
      "wr": 0.857,
      "pnl": 0.0637,
      "score": 5.46
    }
  },
  "ENS": {
    "bull-calm": {
      "RH": 78,
      "RL": 22,
      "sigs": [
        "PV"
      ],
      "flt": "none",
      "TP": 0.02,
      "SL": 0.05,
      "n": 7,
      "wr": 0.714,
      "pnl": 0.0347,
      "score": 2.478
    },
    "bull-storm": {
      "RH": 70,
      "RL": 22,
      "sigs": [
        "PV",
        "BB"
      ],
      "flt": "none",
      "TP": 0.02,
      "SL": 0.05,
      "n": 8,
      "wr": 0.875,
      "pnl": 0.1275,
      "score": 11.156
    },
    "bear-calm": {
      "RH": 78,
      "RL": 30,
      "sigs": [
        "BB"
      ],
      "flt": "none",
      "TP": 0.02,
      "SL": 0.05,
      "n": 8,
      "wr": 0.875,
      "pnl": 0.1146,
      "score": 10.031
    },
    "bear-storm": {
      "RH": 70,
      "RL": 30,
      "sigs": [
        "PV",
        "BB"
      ],
      "flt": "none",
      "TP": 0.006,
      "SL": 0.05,
      "n": 7,
      "wr": 1.0,
      "pnl": 0.0357,
      "score": 3.57
    },
    "chop": {
      "RH": 78,
      "RL": 30,
      "sigs": [
        "PV",
        "BB"
      ],
      "flt": "none",
      "TP": 0.02,
      "SL": 0.05,
      "n": 29,
      "wr": 0.897,
      "pnl": 0.3221,
      "score": 28.879
    }
  },
  "FARTCOIN": {
    "bull-calm": {
      "RH": 78,
      "RL": 30,
      "sigs": [
        "PV",
        "BB"
      ],
      "flt": "none",
      "TP": 0.02,
      "SL": 0.05,
      "n": 25,
      "wr": 0.76,
      "pnl": 0.0575,
      "score": 4.37
    },
    "bull-storm": {
      "RH": 78,
      "RL": 30,
      "sigs": [
        "PV",
        "BB"
      ],
      "flt": "none",
      "TP": 0.012,
      "SL": 0.05,
      "n": 8,
      "wr": 0.875,
      "pnl": 0.0268,
      "score": 2.345
    },
    "bear-calm": {
      "RH": 78,
      "RL": 30,
      "sigs": [
        "PV",
        "BB"
      ],
      "flt": "none",
      "TP": 0.02,
      "SL": 0.05,
      "n": 7,
      "wr": 1.0,
      "pnl": 0.1337,
      "score": 13.37
    },
    "bear-storm": {
      "RH": 70,
      "RL": 30,
      "sigs": [
        "PV",
        "BB"
      ],
      "flt": "none",
      "TP": 0.02,
      "SL": 0.05,
      "n": 7,
      "wr": 0.857,
      "pnl": 0.0637,
      "score": 5.46
    },
    "chop": {
      "RH": 78,
      "RL": 30,
      "sigs": [
        "PV"
      ],
      "flt": "none",
      "TP": 0.02,
      "SL": 0.05,
      "n": 17,
      "wr": 0.882,
      "pnl": 0.1847,
      "score": 16.297
    }
  },
  "FIL": {
    "bull-calm": {
      "RH": 78,
      "RL": 22,
      "sigs": [
        "PV"
      ],
      "flt": "none",
      "TP": 0.02,
      "SL": 0.05,
      "n": 8,
      "wr": 0.375,
      "pnl": -0.1555,
      "score": -5.833
    },
    "bull-storm": {
      "RH": 70,
      "RL": 30,
      "sigs": [
        "PV",
        "BB"
      ],
      "flt": "none",
      "TP": 0.02,
      "SL": 0.05,
      "n": 21,
      "wr": 0.857,
      "pnl": 0.1994,
      "score": 17.095
    },
    "bear-calm": {
      "reason": "no_valid_combo"
    },
    "bear-storm": {
      "RH": 78,
      "RL": 30,
      "sigs": [
        "PV",
        "BB"
      ],
      "flt": "none",
      "TP": 0.012,
      "SL": 0.05,
      "n": 5,
      "wr": 1.0,
      "pnl": 0.0555,
      "score": 5.55
    },
    "chop": {
      "RH": 78,
      "RL": 30,
      "sigs": [
        "BB"
      ],
      "flt": "none",
      "TP": 0.02,
      "SL": 0.05,
      "n": 6,
      "wr": 1.0,
      "pnl": 0.1146,
      "score": 11.46
    }
  },
  "INJ": {
    "bull-calm": {
      "RH": 70,
      "RL": 30,
      "sigs": [
        "PV",
        "BB"
      ],
      "flt": "none",
      "TP": 0.02,
      "SL": 0.05,
      "n": 24,
      "wr": 0.792,
      "pnl": 0.1945,
      "score": 15.395
    },
    "bull-storm": {
      "RH": 70,
      "RL": 30,
      "sigs": [
        "PV",
        "BB"
      ],
      "flt": "none",
      "TP": 0.02,
      "SL": 0.05,
      "n": 21,
      "wr": 0.762,
      "pnl": 0.2421,
      "score": 18.443
    },
    "bear-calm": {
      "RH": 70,
      "RL": 30,
      "sigs": [
        "PV"
      ],
      "flt": "none",
      "TP": 0.012,
      "SL": 0.05,
      "n": 15,
      "wr": 1.0,
      "pnl": 0.16,
      "score": 16.003
    },
    "bear-storm": {
      "RH": 70,
      "RL": 30,
      "sigs": [
        "PV",
        "BB"
      ],
      "flt": "none",
      "TP": 0.006,
      "SL": 0.05,
      "n": 7,
      "wr": 0.857,
      "pnl": 0.0037,
      "score": 0.314
    },
    "chop": {
      "RH": 78,
      "RL": 30,
      "sigs": [
        "PV"
      ],
      "flt": "none",
      "TP": 0.02,
      "SL": 0.05,
      "n": 15,
      "wr": 0.867,
      "pnl": 0.1465,
      "score": 12.697
    }
  },
  "LDO": {
    "bull-calm": {
      "RH": 70,
      "RL": 22,
      "sigs": [
        "PV"
      ],
      "flt": "none",
      "TP": 0.006,
      "SL": 0.05,
      "n": 20,
      "wr": 0.85,
      "pnl": -0.066,
      "score": -5.61
    },
    "bull-storm": {
      "RH": 70,
      "RL": 22,
      "sigs": [
        "PV",
        "BB"
      ],
      "flt": "none",
      "TP": 0.02,
      "SL": 0.05,
      "n": 8,
      "wr": 0.875,
      "pnl": 0.0828,
      "score": 7.245
    },
    "bear-calm": {
      "RH": 70,
      "RL": 30,
      "sigs": [
        "PV"
      ],
      "flt": "none",
      "TP": 0.012,
      "SL": 0.05,
      "n": 15,
      "wr": 0.867,
      "pnl": 0.0686,
      "score": 5.949
    },
    "bear-storm": {
      "RH": 70,
      "RL": 30,
      "sigs": [
        "PV",
        "BB"
      ],
      "flt": "none",
      "TP": 0.006,
      "SL": 0.05,
      "n": 9,
      "wr": 1.0,
      "pnl": 0.0459,
      "score": 4.59
    },
    "chop": {
      "RH": 78,
      "RL": 30,
      "sigs": [
        "PV",
        "BB"
      ],
      "flt": "none",
      "TP": 0.02,
      "SL": 0.05,
      "n": 30,
      "wr": 0.833,
      "pnl": 0.223,
      "score": 18.583
    }
  },
  "LIT": {
    "bull-calm": {
      "RH": 70,
      "RL": 30,
      "sigs": [
        "BB"
      ],
      "flt": "none",
      "TP": 0.012,
      "SL": 0.05,
      "n": 15,
      "wr": 0.867,
      "pnl": 0.0425,
      "score": 3.683
    },
    "bull-storm": {
      "RH": 70,
      "RL": 30,
      "sigs": [
        "PV"
      ],
      "flt": "none",
      "TP": 0.02,
      "SL": 0.05,
      "n": 16,
      "wr": 0.938,
      "pnl": 0.2356,
      "score": 22.087
    },
    "bear-calm": {
      "RH": 70,
      "RL": 30,
      "sigs": [
        "PV"
      ],
      "flt": "none",
      "TP": 0.02,
      "SL": 0.05,
      "n": 9,
      "wr": 0.889,
      "pnl": 0.0886,
      "score": 7.876
    },
    "bear-storm": {
      "RH": 70,
      "RL": 30,
      "sigs": [
        "PV",
        "BB"
      ],
      "flt": "none",
      "TP": 0.012,
      "SL": 0.05,
      "n": 11,
      "wr": 1.0,
      "pnl": 0.1221,
      "score": 12.21
    },
    "chop": {
      "RH": 78,
      "RL": 30,
      "sigs": [
        "PV"
      ],
      "flt": "none",
      "TP": 0.02,
      "SL": 0.05,
      "n": 23,
      "wr": 0.913,
      "pnl": 0.2993,
      "score": 27.327
    }
  },
  "MON": {
    "bull-calm": {
      "RH": 70,
      "RL": 22,
      "sigs": [
        "BB"
      ],
      "flt": "none",
      "TP": 0.012,
      "SL": 0.05,
      "n": 9,
      "wr": 0.889,
      "pnl": 0.0379,
      "score": 3.369
    },
    "bull-storm": {
      "RH": 78,
      "RL": 30,
      "sigs": [
        "BB"
      ],
      "flt": "none",
      "TP": 0.012,
      "SL": 0.05,
      "n": 6,
      "wr": 1.0,
      "pnl": 0.0666,
      "score": 6.66
    },
    "bear-calm": {
      "RH": 78,
      "RL": 30,
      "sigs": [
        "PV",
        "BB"
      ],
      "flt": "none",
      "TP": 0.02,
      "SL": 0.05,
      "n": 11,
      "wr": 0.909,
      "pnl": 0.1401,
      "score": 12.736
    },
    "bear-storm": {
      "RH": 78,
      "RL": 30,
      "sigs": [
        "PV",
        "BB"
      ],
      "flt": "none",
      "TP": 0.02,
      "SL": 0.05,
      "n": 6,
      "wr": 1.0,
      "pnl": 0.1146,
      "score": 11.46
    },
    "chop": {
      "RH": 78,
      "RL": 30,
      "sigs": [
        "BB"
      ],
      "flt": "none",
      "TP": 0.02,
      "SL": 0.05,
      "n": 17,
      "wr": 0.824,
      "pnl": 0.1147,
      "score": 9.446
    }
  },
  "MOODENG": {
    "bull-calm": {
      "RH": 78,
      "RL": 22,
      "sigs": [
        "BB"
      ],
      "flt": "none",
      "TP": 0.012,
      "SL": 0.05,
      "n": 5,
      "wr": 0.8,
      "pnl": -0.0065,
      "score": -0.52
    },
    "bull-storm": {
      "RH": 70,
      "RL": 30,
      "sigs": [
        "PV",
        "BB"
      ],
      "flt": "none",
      "TP": 0.02,
      "SL": 0.05,
      "n": 21,
      "wr": 0.905,
      "pnl": 0.2611,
      "score": 23.623
    },
    "bear-calm": {
      "RH": 78,
      "RL": 30,
      "sigs": [
        "PV"
      ],
      "flt": "none",
      "TP": 0.02,
      "SL": 0.05,
      "n": 8,
      "wr": 1.0,
      "pnl": 0.1422,
      "score": 14.221
    },
    "bear-storm": {
      "RH": 70,
      "RL": 22,
      "sigs": [
        "PV"
      ],
      "flt": "none",
      "TP": 0.02,
      "SL": 0.05,
      "n": 5,
      "wr": 0.2,
      "pnl": -0.1845,
      "score": -3.69
    },
    "chop": {
      "RH": 78,
      "RL": 30,
      "sigs": [
        "PV",
        "BB"
      ],
      "flt": "none",
      "TP": 0.02,
      "SL": 0.05,
      "n": 30,
      "wr": 0.833,
      "pnl": 0.2581,
      "score": 21.512
    }
  },
  "MORPHO": {
    "bull-calm": {
      "RH": 78,
      "RL": 30,
      "sigs": [
        "BB"
      ],
      "flt": "none",
      "TP": 0.02,
      "SL": 0.05,
      "n": 11,
      "wr": 0.909,
      "pnl": 0.1401,
      "score": 12.736
    },
    "bull-storm": {
      "RH": 70,
      "RL": 22,
      "sigs": [
        "PV"
      ],
      "flt": "none",
      "TP": 0.02,
      "SL": 0.05,
      "n": 7,
      "wr": 0.857,
      "pnl": 0.0637,
      "score": 5.46
    },
    "bear-calm": {
      "RH": 70,
      "RL": 30,
      "sigs": [
        "PV",
        "BB"
      ],
      "flt": "none",
      "TP": 0.012,
      "SL": 0.05,
      "n": 13,
      "wr": 1.0,
      "pnl": 0.1443,
      "score": 14.43
    },
    "bear-storm": {
      "RH": 78,
      "RL": 30,
      "sigs": [
        "PV",
        "BB"
      ],
      "flt": "none",
      "TP": 0.02,
      "SL": 0.05,
      "n": 8,
      "wr": 1.0,
      "pnl": 0.1528,
      "score": 15.28
    },
    "chop": {
      "RH": 78,
      "RL": 30,
      "sigs": [
        "PV",
        "BB"
      ],
      "flt": "ema200",
      "TP": 0.02,
      "SL": 0.05,
      "n": 6,
      "wr": 1.0,
      "pnl": 0.1146,
      "score": 11.46
    }
  },
  "OP": {
    "bull-calm": {
      "RH": 70,
      "RL": 22,
      "sigs": [
        "PV"
      ],
      "flt": "none",
      "TP": 0.02,
      "SL": 0.05,
      "n": 11,
      "wr": 0.909,
      "pnl": 0.1401,
      "score": 12.736
    },
    "bull-storm": {
      "RH": 70,
      "RL": 22,
      "sigs": [
        "PV",
        "BB"
      ],
      "flt": "none",
      "TP": 0.02,
      "SL": 0.05,
      "n": 10,
      "wr": 1.0,
      "pnl": 0.1774,
      "score": 17.74
    },
    "bear-calm": {
      "RH": 70,
      "RL": 30,
      "sigs": [
        "PV",
        "BB"
      ],
      "flt": "none",
      "TP": 0.02,
      "SL": 0.05,
      "n": 10,
      "wr": 0.9,
      "pnl": 0.1259,
      "score": 11.331
    },
    "bear-storm": {
      "RH": 70,
      "RL": 30,
      "sigs": [
        "PV",
        "BB"
      ],
      "flt": "none",
      "TP": 0.006,
      "SL": 0.05,
      "n": 7,
      "wr": 0.857,
      "pnl": -0.0203,
      "score": -1.74
    },
    "chop": {
      "RH": 78,
      "RL": 30,
      "sigs": [
        "BB"
      ],
      "flt": "none",
      "TP": 0.02,
      "SL": 0.05,
      "n": 20,
      "wr": 0.85,
      "pnl": 0.172,
      "score": 14.62
    }
  },
  "ORDI": {
    "bull-calm": {
      "RH": 78,
      "RL": 30,
      "sigs": [
        "PV",
        "BB"
      ],
      "flt": "none",
      "TP": 0.02,
      "SL": 0.05,
      "n": 17,
      "wr": 0.882,
      "pnl": 0.1847,
      "score": 16.297
    },
    "bull-storm": {
      "RH": 70,
      "RL": 22,
      "sigs": [
        "PV",
        "BB"
      ],
      "flt": "none",
      "TP": 0.02,
      "SL": 0.05,
      "n": 9,
      "wr": 0.778,
      "pnl": 0.0319,
      "score": 2.481
    },
    "bear-calm": {
      "reason": "no_valid_combo"
    },
    "bear-storm": {
      "RH": 78,
      "RL": 22,
      "sigs": [
        "PV"
      ],
      "flt": "none",
      "TP": 0.02,
      "SL": 0.05,
      "n": 5,
      "wr": 0.4,
      "pnl": -0.1145,
      "score": -4.58
    },
    "chop": {
      "RH": 78,
      "RL": 30,
      "sigs": [
        "BB"
      ],
      "flt": "none",
      "TP": 0.012,
      "SL": 0.05,
      "n": 17,
      "wr": 0.882,
      "pnl": 0.0647,
      "score": 5.709
    }
  },
  "PENDLE": {
    "bull-calm": {
      "RH": 78,
      "RL": 30,
      "sigs": [
        "BB"
      ],
      "flt": "none",
      "TP": 0.02,
      "SL": 0.05,
      "n": 11,
      "wr": 0.909,
      "pnl": 0.1401,
      "score": 12.736
    },
    "bull-storm": {
      "RH": 70,
      "RL": 30,
      "sigs": [
        "PV",
        "BB"
      ],
      "flt": "none",
      "TP": 0.02,
      "SL": 0.05,
      "n": 20,
      "wr": 0.85,
      "pnl": 0.2111,
      "score": 17.944
    },
    "bear-calm": {
      "RH": 78,
      "RL": 22,
      "sigs": [
        "PV",
        "BB"
      ],
      "flt": "none",
      "TP": 0.02,
      "SL": 0.05,
      "n": 5,
      "wr": 1.0,
      "pnl": 0.089,
      "score": 8.896
    },
    "bear-storm": {
      "RH": 78,
      "RL": 30,
      "sigs": [
        "PV"
      ],
      "flt": "none",
      "TP": 0.02,
      "SL": 0.05,
      "n": 5,
      "wr": 1.0,
      "pnl": 0.0955,
      "score": 9.55
    },
    "chop": {
      "RH": 78,
      "RL": 30,
      "sigs": [
        "PV",
        "BB"
      ],
      "flt": "ema200",
      "TP": 0.006,
      "SL": 0.05,
      "n": 6,
      "wr": 1.0,
      "pnl": 0.0306,
      "score": 3.06
    }
  },
  "PENGU": {
    "bull-calm": {
      "RH": 70,
      "RL": 22,
      "sigs": [
        "BB"
      ],
      "flt": "none",
      "TP": 0.02,
      "SL": 0.05,
      "n": 9,
      "wr": 1.0,
      "pnl": 0.1719,
      "score": 17.19
    },
    "bull-storm": {
      "RH": 78,
      "RL": 30,
      "sigs": [
        "BB"
      ],
      "flt": "none",
      "TP": 0.02,
      "SL": 0.05,
      "n": 8,
      "wr": 0.875,
      "pnl": 0.0828,
      "score": 7.245
    },
    "bear-calm": {
      "RH": 78,
      "RL": 30,
      "sigs": [
        "BB"
      ],
      "flt": "none",
      "TP": 0.02,
      "SL": 0.05,
      "n": 6,
      "wr": 1.0,
      "pnl": 0.1146,
      "score": 11.46
    },
    "bear-storm": {
      "RH": 78,
      "RL": 30,
      "sigs": [
        "PV",
        "BB"
      ],
      "flt": "none",
      "TP": 0.006,
      "SL": 0.05,
      "n": 5,
      "wr": 1.0,
      "pnl": 0.0255,
      "score": 2.55
    },
    "chop": {
      "RH": 78,
      "RL": 30,
      "sigs": [
        "PV"
      ],
      "flt": "none",
      "TP": 0.02,
      "SL": 0.05,
      "n": 20,
      "wr": 0.9,
      "pnl": 0.242,
      "score": 21.78
    }
  },
  "POL": {
    "bull-calm": {
      "RH": 78,
      "RL": 22,
      "sigs": [
        "PV",
        "BB"
      ],
      "flt": "none",
      "TP": 0.012,
      "SL": 0.05,
      "n": 7,
      "wr": 1.0,
      "pnl": 0.0777,
      "score": 7.77
    },
    "bull-storm": {
      "RH": 70,
      "RL": 22,
      "sigs": [
        "PV",
        "BB"
      ],
      "flt": "none",
      "TP": 0.02,
      "SL": 0.05,
      "n": 9,
      "wr": 0.778,
      "pnl": 0.0819,
      "score": 6.367
    },
    "bear-calm": {
      "RH": 70,
      "RL": 30,
      "sigs": [
        "PV"
      ],
      "flt": "none",
      "TP": 0.006,
      "SL": 0.05,
      "n": 14,
      "wr": 0.786,
      "pnl": -0.0502,
      "score": -3.941
    },
    "bear-storm": {
      "RH": 78,
      "RL": 30,
      "sigs": [
        "PV"
      ],
      "flt": "none",
      "TP": 0.012,
      "SL": 0.05,
      "n": 5,
      "wr": 1.0,
      "pnl": 0.0555,
      "score": 5.55
    },
    "chop": {
      "RH": 70,
      "RL": 30,
      "sigs": [
        "PV",
        "BB"
      ],
      "flt": "none",
      "TP": 0.02,
      "SL": 0.05,
      "n": 52,
      "wr": 0.885,
      "pnl": 0.5703,
      "score": 50.449
    }
  },
  "SOL": {
    "bull-calm": {
      "RH": 70,
      "RL": 30,
      "sigs": [
        "BB"
      ],
      "flt": "none",
      "TP": 0.02,
      "SL": 0.05,
      "n": 13,
      "wr": 0.769,
      "pnl": 0.0803,
      "score": 6.176
    },
    "bull-storm": {
      "RH": 70,
      "RL": 30,
      "sigs": [
        "PV",
        "BB"
      ],
      "flt": "none",
      "TP": 0.02,
      "SL": 0.05,
      "n": 14,
      "wr": 0.929,
      "pnl": 0.2407,
      "score": 22.349
    },
    "bear-calm": {
      "RH": 70,
      "RL": 30,
      "sigs": [
        "PV"
      ],
      "flt": "none",
      "TP": 0.02,
      "SL": 0.05,
      "n": 10,
      "wr": 0.9,
      "pnl": 0.1304,
      "score": 11.737
    },
    "bear-storm": {
      "RH": 70,
      "RL": 30,
      "sigs": [
        "PV",
        "BB"
      ],
      "flt": "none",
      "TP": 0.006,
      "SL": 0.05,
      "n": 7,
      "wr": 0.857,
      "pnl": -0.0203,
      "score": -1.74
    },
    "chop": {
      "RH": 78,
      "RL": 30,
      "sigs": [
        "PV",
        "BB"
      ],
      "flt": "none",
      "TP": 0.02,
      "SL": 0.05,
      "n": 32,
      "wr": 0.938,
      "pnl": 0.4712,
      "score": 44.175
    }
  },
  "SPX": {
    "bull-calm": {
      "RH": 70,
      "RL": 22,
      "sigs": [
        "BB"
      ],
      "flt": "none",
      "TP": 0.006,
      "SL": 0.05,
      "n": 11,
      "wr": 0.909,
      "pnl": 0.0001,
      "score": 0.009
    },
    "bull-storm": {
      "RH": 78,
      "RL": 30,
      "sigs": [
        "BB"
      ],
      "flt": "none",
      "TP": 0.02,
      "SL": 0.05,
      "n": 7,
      "wr": 1.0,
      "pnl": 0.1337,
      "score": 13.37
    },
    "bear-calm": {
      "RH": 70,
      "RL": 30,
      "sigs": [
        "PV"
      ],
      "flt": "none",
      "TP": 0.02,
      "SL": 0.05,
      "n": 13,
      "wr": 0.769,
      "pnl": 0.0781,
      "score": 6.008
    },
    "bear-storm": {
      "RH": 70,
      "RL": 30,
      "sigs": [
        "PV",
        "BB"
      ],
      "flt": "none",
      "TP": 0.006,
      "SL": 0.05,
      "n": 5,
      "wr": 1.0,
      "pnl": 0.0255,
      "score": 2.55
    },
    "chop": {
      "RH": 78,
      "RL": 30,
      "sigs": [
        "BB"
      ],
      "flt": "none",
      "TP": 0.02,
      "SL": 0.05,
      "n": 16,
      "wr": 0.812,
      "pnl": 0.0956,
      "score": 7.767
    }
  },
  "TIA": {
    "bull-calm": {
      "RH": 78,
      "RL": 30,
      "sigs": [
        "BB"
      ],
      "flt": "none",
      "TP": 0.012,
      "SL": 0.05,
      "n": 9,
      "wr": 1.0,
      "pnl": 0.0999,
      "score": 9.99
    },
    "bull-storm": {
      "RH": 70,
      "RL": 22,
      "sigs": [
        "PV",
        "BB"
      ],
      "flt": "none",
      "TP": 0.02,
      "SL": 0.05,
      "n": 12,
      "wr": 0.833,
      "pnl": 0.1695,
      "score": 14.122
    },
    "bear-calm": {
      "RH": 70,
      "RL": 30,
      "sigs": [
        "PV"
      ],
      "flt": "none",
      "TP": 0.02,
      "SL": 0.05,
      "n": 12,
      "wr": 0.833,
      "pnl": 0.1327,
      "score": 11.062
    },
    "bear-storm": {
      "RH": 70,
      "RL": 30,
      "sigs": [
        "PV",
        "BB"
      ],
      "flt": "none",
      "TP": 0.006,
      "SL": 0.05,
      "n": 7,
      "wr": 0.857,
      "pnl": -0.0203,
      "score": -1.74
    },
    "chop": {
      "RH": 78,
      "RL": 30,
      "sigs": [
        "PV"
      ],
      "flt": "none",
      "TP": 0.02,
      "SL": 0.05,
      "n": 18,
      "wr": 0.889,
      "pnl": 0.1958,
      "score": 17.406
    }
  },
  "TON": {
    "bull-calm": {
      "RH": 70,
      "RL": 30,
      "sigs": [
        "PV",
        "BB"
      ],
      "flt": "ema200",
      "TP": 0.02,
      "SL": 0.05,
      "n": 6,
      "wr": 1.0,
      "pnl": 0.1007,
      "score": 10.073
    },
    "bull-storm": {
      "RH": 78,
      "RL": 30,
      "sigs": [
        "PV"
      ],
      "flt": "none",
      "TP": 0.02,
      "SL": 0.05,
      "n": 14,
      "wr": 0.714,
      "pnl": -0.0126,
      "score": -0.9
    },
    "bear-calm": {
      "RH": 78,
      "RL": 30,
      "sigs": [
        "PV",
        "BB"
      ],
      "flt": "none",
      "TP": 0.02,
      "SL": 0.05,
      "n": 9,
      "wr": 0.889,
      "pnl": 0.1164,
      "score": 10.349
    },
    "bear-storm": {
      "RH": 70,
      "RL": 22,
      "sigs": [
        "PV",
        "BB"
      ],
      "flt": "none",
      "TP": 0.012,
      "SL": 0.05,
      "n": 5,
      "wr": 1.0,
      "pnl": 0.0555,
      "score": 5.55
    },
    "chop": {
      "RH": 70,
      "RL": 30,
      "sigs": [
        "BB"
      ],
      "flt": "none",
      "TP": 0.012,
      "SL": 0.05,
      "n": 27,
      "wr": 0.852,
      "pnl": 0.1172,
      "score": 9.98
    }
  },
  "TURBO": {
    "bull-calm": {
      "RH": 70,
      "RL": 22,
      "sigs": [
        "PV"
      ],
      "flt": "none",
      "TP": 0.006,
      "SL": 0.05,
      "n": 9,
      "wr": 1.0,
      "pnl": 0.0459,
      "score": 4.59
    },
    "bull-storm": {
      "RH": 70,
      "RL": 30,
      "sigs": [
        "PV",
        "BB"
      ],
      "flt": "none",
      "TP": 0.02,
      "SL": 0.05,
      "n": 13,
      "wr": 0.923,
      "pnl": 0.1705,
      "score": 15.734
    },
    "bear-calm": {
      "reason": "no_valid_combo"
    },
    "bear-storm": {
      "reason": "no_valid_combo"
    },
    "chop": {
      "RH": 78,
      "RL": 30,
      "sigs": [
        "PV"
      ],
      "flt": "ema200",
      "TP": 0.02,
      "SL": 0.05,
      "n": 6,
      "wr": 1.0,
      "pnl": 0.1146,
      "score": 11.46
    }
  },
  "UMA": {
    "bull-calm": {
      "RH": 78,
      "RL": 30,
      "sigs": [
        "BB"
      ],
      "flt": "none",
      "TP": 0.012,
      "SL": 0.05,
      "n": 8,
      "wr": 0.875,
      "pnl": 0.0268,
      "score": 2.345
    },
    "bull-storm": {
      "RH": 70,
      "RL": 22,
      "sigs": [
        "PV",
        "BB"
      ],
      "flt": "none",
      "TP": 0.02,
      "SL": 0.05,
      "n": 9,
      "wr": 0.889,
      "pnl": 0.1453,
      "score": 12.919
    },
    "bear-calm": {
      "RH": 78,
      "RL": 30,
      "sigs": [
        "PV"
      ],
      "flt": "none",
      "TP": 0.02,
      "SL": 0.05,
      "n": 5,
      "wr": 1.0,
      "pnl": 0.0955,
      "score": 9.55
    },
    "bear-storm": {
      "RH": 78,
      "RL": 30,
      "sigs": [
        "PV"
      ],
      "flt": "none",
      "TP": 0.02,
      "SL": 0.05,
      "n": 8,
      "wr": 1.0,
      "pnl": 0.1528,
      "score": 15.28
    },
    "chop": {
      "RH": 78,
      "RL": 30,
      "sigs": [
        "PV",
        "BB"
      ],
      "flt": "none",
      "TP": 0.012,
      "SL": 0.05,
      "n": 22,
      "wr": 0.909,
      "pnl": 0.1202,
      "score": 10.927
    }
  },
  "UNI": {
    "bull-calm": {
      "RH": 70,
      "RL": 22,
      "sigs": [
        "BB"
      ],
      "flt": "none",
      "TP": 0.02,
      "SL": 0.05,
      "n": 6,
      "wr": 0.833,
      "pnl": 0.0674,
      "score": 5.617
    },
    "bull-storm": {
      "RH": 70,
      "RL": 22,
      "sigs": [
        "PV",
        "BB"
      ],
      "flt": "none",
      "TP": 0.02,
      "SL": 0.05,
      "n": 7,
      "wr": 1.0,
      "pnl": 0.1187,
      "score": 11.872
    },
    "bear-calm": {
      "RH": 70,
      "RL": 30,
      "sigs": [
        "PV"
      ],
      "flt": "none",
      "TP": 0.02,
      "SL": 0.05,
      "n": 11,
      "wr": 0.909,
      "pnl": 0.1727,
      "score": 15.698
    },
    "bear-storm": {
      "RH": 70,
      "RL": 30,
      "sigs": [
        "BB"
      ],
      "flt": "none",
      "TP": 0.006,
      "SL": 0.05,
      "n": 6,
      "wr": 1.0,
      "pnl": 0.0306,
      "score": 3.06
    },
    "chop": {
      "RH": 78,
      "RL": 30,
      "sigs": [
        "PV"
      ],
      "flt": "none",
      "TP": 0.02,
      "SL": 0.05,
      "n": 12,
      "wr": 1.0,
      "pnl": 0.2292,
      "score": 22.92
    }
  },
  "WIF": {
    "bull-calm": {
      "RH": 70,
      "RL": 22,
      "sigs": [
        "BB"
      ],
      "flt": "none",
      "TP": 0.02,
      "SL": 0.05,
      "n": 7,
      "wr": 1.0,
      "pnl": 0.1337,
      "score": 13.37
    },
    "bull-storm": {
      "RH": 70,
      "RL": 30,
      "sigs": [
        "PV",
        "BB"
      ],
      "flt": "none",
      "TP": 0.02,
      "SL": 0.05,
      "n": 18,
      "wr": 0.944,
      "pnl": 0.2922,
      "score": 27.597
    },
    "bear-calm": {
      "RH": 70,
      "RL": 30,
      "sigs": [
        "PV"
      ],
      "flt": "none",
      "TP": 0.02,
      "SL": 0.05,
      "n": 10,
      "wr": 0.9,
      "pnl": 0.1487,
      "score": 13.381
    },
    "bear-storm": {
      "RH": 70,
      "RL": 30,
      "sigs": [
        "PV",
        "BB"
      ],
      "flt": "none",
      "TP": 0.02,
      "SL": 0.05,
      "n": 6,
      "wr": 0.833,
      "pnl": 0.0446,
      "score": 3.717
    },
    "chop": {
      "RH": 78,
      "RL": 30,
      "sigs": [
        "PV",
        "BB"
      ],
      "flt": "none",
      "TP": 0.02,
      "SL": 0.05,
      "n": 33,
      "wr": 0.879,
      "pnl": 0.3503,
      "score": 30.784
    }
  },
  "WLD": {
    "bull-calm": {
      "RH": 70,
      "RL": 30,
      "sigs": [
        "PV"
      ],
      "flt": "none",
      "TP": 0.012,
      "SL": 0.05,
      "n": 27,
      "wr": 0.852,
      "pnl": 0.0917,
      "score": 7.811
    },
    "bull-storm": {
      "RH": 70,
      "RL": 30,
      "sigs": [
        "PV",
        "BB"
      ],
      "flt": "none",
      "TP": 0.02,
      "SL": 0.05,
      "n": 18,
      "wr": 0.944,
      "pnl": 0.2738,
      "score": 25.859
    },
    "bear-calm": {
      "RH": 70,
      "RL": 30,
      "sigs": [
        "PV"
      ],
      "flt": "none",
      "TP": 0.02,
      "SL": 0.05,
      "n": 9,
      "wr": 0.889,
      "pnl": 0.1424,
      "score": 12.654
    },
    "bear-storm": {
      "RH": 70,
      "RL": 30,
      "sigs": [
        "BB"
      ],
      "flt": "none",
      "TP": 0.02,
      "SL": 0.05,
      "n": 5,
      "wr": 1.0,
      "pnl": 0.0955,
      "score": 9.55
    },
    "chop": {
      "RH": 70,
      "RL": 30,
      "sigs": [
        "PV",
        "BB"
      ],
      "flt": "ema200",
      "TP": 0.006,
      "SL": 0.05,
      "n": 9,
      "wr": 1.0,
      "pnl": 0.0459,
      "score": 4.59
    }
  },
  "ANIME": {
    "bull-calm": {
      "RH": 70,
      "RL": 22,
      "sigs": [
        "PV",
        "BB"
      ],
      "flt": "none",
      "TP": 0.02,
      "SL": 0.05,
      "n": 20,
      "wr": 0.75,
      "pnl": 0.0872,
      "score": 6.543
    },
    "bull-storm": {
      "RH": 70,
      "RL": 22,
      "sigs": [
        "PV"
      ],
      "flt": "none",
      "TP": 0.02,
      "SL": 0.05,
      "n": 8,
      "wr": 0.875,
      "pnl": 0.1114,
      "score": 9.746
    },
    "bear-calm": {
      "reason": "no_valid_combo"
    },
    "bear-storm": {
      "RH": 70,
      "RL": 22,
      "sigs": [
        "PV",
        "BB"
      ],
      "flt": "none",
      "TP": 0.02,
      "SL": 0.05,
      "n": 5,
      "wr": 0.0,
      "pnl": -0.0901,
      "score": -0.0
    },
    "chop": {
      "RH": 78,
      "RL": 30,
      "sigs": [
        "PV",
        "BB"
      ],
      "flt": "none",
      "TP": 0.02,
      "SL": 0.05,
      "n": 19,
      "wr": 0.895,
      "pnl": 0.2229,
      "score": 19.944
    }
  },
  "APEX": {
    "bull-calm": {
      "RH": 70,
      "RL": 22,
      "sigs": [
        "BB"
      ],
      "flt": "none",
      "TP": 0.02,
      "SL": 0.05,
      "n": 5,
      "wr": 0.8,
      "pnl": 0.0255,
      "score": 2.04
    },
    "bull-storm": {
      "RH": 70,
      "RL": 30,
      "sigs": [
        "PV"
      ],
      "flt": "none",
      "TP": 0.02,
      "SL": 0.05,
      "n": 15,
      "wr": 0.867,
      "pnl": 0.1847,
      "score": 16.01
    },
    "bear-calm": {
      "reason": "no_valid_combo"
    },
    "bear-storm": {
      "reason": "no_valid_combo"
    },
    "chop": {
      "RH": 70,
      "RL": 30,
      "sigs": [
        "PV",
        "BB"
      ],
      "flt": "ema200",
      "TP": 0.012,
      "SL": 0.05,
      "n": 7,
      "wr": 1.0,
      "pnl": 0.0777,
      "score": 7.77
    }
  },
  "AXS": {
    "bull-calm": {
      "RH": 70,
      "RL": 30,
      "sigs": [
        "PV",
        "BB"
      ],
      "flt": "ema200",
      "TP": 0.02,
      "SL": 0.05,
      "n": 5,
      "wr": 1.0,
      "pnl": 0.0955,
      "score": 9.55
    },
    "bull-storm": {
      "RH": 70,
      "RL": 22,
      "sigs": [
        "PV"
      ],
      "flt": "none",
      "TP": 0.02,
      "SL": 0.05,
      "n": 5,
      "wr": 0.8,
      "pnl": 0.0533,
      "score": 4.261
    },
    "bear-calm": {
      "reason": "no_valid_combo"
    },
    "bear-storm": {
      "RH": 78,
      "RL": 30,
      "sigs": [
        "PV",
        "BB"
      ],
      "flt": "none",
      "TP": 0.02,
      "SL": 0.05,
      "n": 6,
      "wr": 0.833,
      "pnl": 0.0446,
      "score": 3.717
    },
    "chop": {
      "RH": 78,
      "RL": 30,
      "sigs": [
        "PV",
        "BB"
      ],
      "flt": "none",
      "TP": 0.02,
      "SL": 0.05,
      "n": 23,
      "wr": 0.913,
      "pnl": 0.347,
      "score": 31.68
    }
  },
  "BCH": {
    "bull-calm": {
      "RH": 70,
      "RL": 22,
      "sigs": [
        "PV"
      ],
      "flt": "none",
      "TP": 0.02,
      "SL": 0.05,
      "n": 12,
      "wr": 0.667,
      "pnl": 0.0958,
      "score": 6.385
    },
    "bull-storm": {
      "RH": 70,
      "RL": 22,
      "sigs": [
        "PV",
        "BB"
      ],
      "flt": "none",
      "TP": 0.02,
      "SL": 0.05,
      "n": 7,
      "wr": 0.714,
      "pnl": 0.0505,
      "score": 3.608
    },
    "bear-calm": {
      "reason": "no_valid_combo"
    },
    "bear-storm": {
      "RH": 70,
      "RL": 22,
      "sigs": [
        "PV",
        "BB"
      ],
      "flt": "none",
      "TP": 0.012,
      "SL": 0.05,
      "n": 5,
      "wr": 0.0,
      "pnl": -0.1285,
      "score": -0.0
    },
    "chop": {
      "RH": 78,
      "RL": 30,
      "sigs": [
        "PV",
        "BB"
      ],
      "flt": "none",
      "TP": 0.02,
      "SL": 0.05,
      "n": 30,
      "wr": 0.8,
      "pnl": 0.3582,
      "score": 28.659
    }
  },
  "CC": {
    "bull-calm": {
      "RH": 78,
      "RL": 22,
      "sigs": [
        "PV"
      ],
      "flt": "none",
      "TP": 0.02,
      "SL": 0.05,
      "n": 10,
      "wr": 0.9,
      "pnl": 0.1112,
      "score": 10.012
    },
    "bull-storm": {
      "RH": 70,
      "RL": 30,
      "sigs": [
        "BB"
      ],
      "flt": "none",
      "TP": 0.006,
      "SL": 0.05,
      "n": 8,
      "wr": 1.0,
      "pnl": 0.0408,
      "score": 4.08
    },
    "bear-calm": {
      "reason": "no_valid_combo"
    },
    "bear-storm": {
      "RH": 78,
      "RL": 30,
      "sigs": [
        "PV",
        "BB"
      ],
      "flt": "none",
      "TP": 0.006,
      "SL": 0.05,
      "n": 7,
      "wr": 1.0,
      "pnl": 0.0357,
      "score": 3.57
    },
    "chop": {
      "RH": 78,
      "RL": 22,
      "sigs": [
        "PV"
      ],
      "flt": "none",
      "TP": 0.02,
      "SL": 0.05,
      "n": 14,
      "wr": 1.0,
      "pnl": 0.2674,
      "score": 26.74
    }
  },
  "CELO": {
    "bull-calm": {
      "RH": 70,
      "RL": 30,
      "sigs": [
        "PV",
        "BB"
      ],
      "flt": "none",
      "TP": 0.02,
      "SL": 0.05,
      "n": 29,
      "wr": 0.724,
      "pnl": 0.1808,
      "score": 13.095
    },
    "bull-storm": {
      "RH": 70,
      "RL": 22,
      "sigs": [
        "PV",
        "BB"
      ],
      "flt": "none",
      "TP": 0.02,
      "SL": 0.05,
      "n": 11,
      "wr": 0.909,
      "pnl": 0.1602,
      "score": 14.565
    },
    "bear-calm": {
      "reason": "no_valid_combo"
    },
    "bear-storm": {
      "RH": 78,
      "RL": 30,
      "sigs": [
        "PV"
      ],
      "flt": "none",
      "TP": 0.02,
      "SL": 0.05,
      "n": 6,
      "wr": 1.0,
      "pnl": 0.1146,
      "score": 11.46
    },
    "chop": {
      "RH": 78,
      "RL": 30,
      "sigs": [
        "PV",
        "BB"
      ],
      "flt": "none",
      "TP": 0.02,
      "SL": 0.05,
      "n": 24,
      "wr": 0.875,
      "pnl": 0.2177,
      "score": 19.049
    }
  },
  "GMT": {
    "bull-calm": {
      "RH": 70,
      "RL": 22,
      "sigs": [
        "PV"
      ],
      "flt": "none",
      "TP": 0.02,
      "SL": 0.05,
      "n": 10,
      "wr": 1.0,
      "pnl": 0.191,
      "score": 19.1
    },
    "bull-storm": {
      "RH": 70,
      "RL": 30,
      "sigs": [
        "PV",
        "BB"
      ],
      "flt": "none",
      "TP": 0.02,
      "SL": 0.05,
      "n": 22,
      "wr": 0.818,
      "pnl": 0.2713,
      "score": 22.195
    },
    "bear-calm": {
      "reason": "no_valid_combo"
    },
    "bear-storm": {
      "RH": 70,
      "RL": 30,
      "sigs": [
        "PV",
        "BB"
      ],
      "flt": "none",
      "TP": 0.012,
      "SL": 0.05,
      "n": 5,
      "wr": 1.0,
      "pnl": 0.0555,
      "score": 5.55
    },
    "chop": {
      "RH": 78,
      "RL": 30,
      "sigs": [
        "PV",
        "BB"
      ],
      "flt": "none",
      "TP": 0.012,
      "SL": 0.05,
      "n": 15,
      "wr": 0.867,
      "pnl": 0.0425,
      "score": 3.683
    }
  },
  "HEMI": {
    "bull-calm": {
      "RH": 70,
      "RL": 30,
      "sigs": [
        "BB"
      ],
      "flt": "none",
      "TP": 0.02,
      "SL": 0.05,
      "n": 14,
      "wr": 0.786,
      "pnl": 0.0879,
      "score": 6.904
    },
    "bull-storm": {
      "RH": 70,
      "RL": 22,
      "sigs": [
        "PV"
      ],
      "flt": "none",
      "TP": 0.02,
      "SL": 0.05,
      "n": 8,
      "wr": 1.0,
      "pnl": 0.1528,
      "score": 15.28
    },
    "bear-calm": {
      "reason": "no_valid_combo"
    },
    "bear-storm": {
      "RH": 70,
      "RL": 30,
      "sigs": [
        "PV"
      ],
      "flt": "none",
      "TP": 0.012,
      "SL": 0.05,
      "n": 5,
      "wr": 1.0,
      "pnl": 0.0555,
      "score": 5.55
    },
    "chop": {
      "RH": 70,
      "RL": 30,
      "sigs": [
        "PV",
        "BB"
      ],
      "flt": "none",
      "TP": 0.012,
      "SL": 0.05,
      "n": 42,
      "wr": 0.857,
      "pnl": 0.0942,
      "score": 8.074
    }
  },
  "INIT": {
    "bull-calm": {
      "RH": 78,
      "RL": 30,
      "sigs": [
        "PV",
        "BB"
      ],
      "flt": "none",
      "TP": 0.02,
      "SL": 0.05,
      "n": 14,
      "wr": 0.857,
      "pnl": 0.1274,
      "score": 10.92
    },
    "bull-storm": {
      "RH": 70,
      "RL": 22,
      "sigs": [
        "PV",
        "BB"
      ],
      "flt": "none",
      "TP": 0.02,
      "SL": 0.05,
      "n": 8,
      "wr": 0.875,
      "pnl": 0.0828,
      "score": 7.245
    },
    "bear-calm": {
      "reason": "no_valid_combo"
    },
    "bear-storm": {
      "RH": 78,
      "RL": 30,
      "sigs": [
        "PV",
        "BB"
      ],
      "flt": "none",
      "TP": 0.02,
      "SL": 0.05,
      "n": 7,
      "wr": 1.0,
      "pnl": 0.1337,
      "score": 13.37
    },
    "chop": {
      "RH": 78,
      "RL": 30,
      "sigs": [
        "BB"
      ],
      "flt": "none",
      "TP": 0.02,
      "SL": 0.05,
      "n": 13,
      "wr": 0.769,
      "pnl": 0.0383,
      "score": 2.946
    }
  },
  "KAS": {
    "bull-calm": {
      "RH": 70,
      "RL": 30,
      "sigs": [
        "PV",
        "BB"
      ],
      "flt": "none",
      "TP": 0.02,
      "SL": 0.05,
      "n": 23,
      "wr": 0.783,
      "pnl": 0.1918,
      "score": 15.01
    },
    "bull-storm": {
      "RH": 70,
      "RL": 22,
      "sigs": [
        "PV"
      ],
      "flt": "none",
      "TP": 0.02,
      "SL": 0.05,
      "n": 9,
      "wr": 0.889,
      "pnl": 0.1409,
      "score": 12.521
    },
    "bear-calm": {
      "reason": "no_valid_combo"
    },
    "bear-storm": {
      "RH": 78,
      "RL": 30,
      "sigs": [
        "PV",
        "BB"
      ],
      "flt": "none",
      "TP": 0.02,
      "SL": 0.05,
      "n": 6,
      "wr": 1.0,
      "pnl": 0.1146,
      "score": 11.46
    },
    "chop": {
      "RH": 78,
      "RL": 30,
      "sigs": [
        "BB"
      ],
      "flt": "none",
      "TP": 0.006,
      "SL": 0.05,
      "n": 11,
      "wr": 0.909,
      "pnl": 0.0001,
      "score": 0.009
    }
  },
  "MANTA": {
    "bull-calm": {
      "RH": 78,
      "RL": 22,
      "sigs": [
        "PV"
      ],
      "flt": "none",
      "TP": 0.012,
      "SL": 0.05,
      "n": 10,
      "wr": 0.8,
      "pnl": 0.019,
      "score": 1.522
    },
    "bull-storm": {
      "RH": 78,
      "RL": 30,
      "sigs": [
        "BB"
      ],
      "flt": "none",
      "TP": 0.02,
      "SL": 0.05,
      "n": 7,
      "wr": 0.857,
      "pnl": 0.0945,
      "score": 8.097
    },
    "bear-calm": {
      "reason": "no_valid_combo"
    },
    "bear-storm": {
      "RH": 70,
      "RL": 30,
      "sigs": [
        "PV",
        "BB"
      ],
      "flt": "none",
      "TP": 0.02,
      "SL": 0.05,
      "n": 7,
      "wr": 0.857,
      "pnl": 0.1137,
      "score": 9.746
    },
    "chop": {
      "RH": 70,
      "RL": 30,
      "sigs": [
        "PV"
      ],
      "flt": "none",
      "TP": 0.012,
      "SL": 0.05,
      "n": 30,
      "wr": 0.9,
      "pnl": 0.1916,
      "score": 17.245
    }
  },
  "MET": {
    "bull-calm": {
      "RH": 78,
      "RL": 30,
      "sigs": [
        "PV",
        "BB"
      ],
      "flt": "none",
      "TP": 0.02,
      "SL": 0.05,
      "n": 22,
      "wr": 0.773,
      "pnl": 0.138,
      "score": 10.663
    },
    "bull-storm": {
      "RH": 78,
      "RL": 30,
      "sigs": [
        "PV",
        "BB"
      ],
      "flt": "none",
      "TP": 0.012,
      "SL": 0.05,
      "n": 17,
      "wr": 0.824,
      "pnl": 0.0373,
      "score": 3.068
    },
    "bear-calm": {
      "reason": "no_valid_combo"
    },
    "bear-storm": {
      "RH": 70,
      "RL": 30,
      "sigs": [
        "PV"
      ],
      "flt": "none",
      "TP": 0.012,
      "SL": 0.05,
      "n": 9,
      "wr": 0.889,
      "pnl": 0.0815,
      "score": 7.246
    },
    "chop": {
      "RH": 78,
      "RL": 22,
      "sigs": [
        "PV"
      ],
      "flt": "none",
      "TP": 0.012,
      "SL": 0.05,
      "n": 6,
      "wr": 1.0,
      "pnl": 0.0666,
      "score": 6.66
    }
  },
  "NXPC": {
    "bull-calm": {
      "RH": 78,
      "RL": 30,
      "sigs": [
        "PV"
      ],
      "flt": "none",
      "TP": 0.02,
      "SL": 0.05,
      "n": 11,
      "wr": 0.818,
      "pnl": 0.1589,
      "score": 13.004
    },
    "bull-storm": {
      "RH": 70,
      "RL": 22,
      "sigs": [
        "PV",
        "BB"
      ],
      "flt": "none",
      "TP": 0.012,
      "SL": 0.05,
      "n": 5,
      "wr": 1.0,
      "pnl": 0.0555,
      "score": 5.55
    },
    "bear-calm": {
      "reason": "no_valid_combo"
    },
    "bear-storm": {
      "RH": 78,
      "RL": 30,
      "sigs": [
        "PV",
        "BB"
      ],
      "flt": "none",
      "TP": 0.02,
      "SL": 0.05,
      "n": 5,
      "wr": 0.8,
      "pnl": 0.0536,
      "score": 4.288
    },
    "chop": {
      "RH": 78,
      "RL": 30,
      "sigs": [
        "PV",
        "BB"
      ],
      "flt": "none",
      "TP": 0.02,
      "SL": 0.05,
      "n": 19,
      "wr": 0.895,
      "pnl": 0.2219,
      "score": 19.857
    }
  },
  "POPCAT": {
    "bull-calm": {
      "RH": 70,
      "RL": 30,
      "sigs": [
        "PV",
        "BB"
      ],
      "flt": "none",
      "TP": 0.02,
      "SL": 0.05,
      "n": 29,
      "wr": 0.862,
      "pnl": 0.2739,
      "score": 23.612
    },
    "bull-storm": {
      "RH": 70,
      "RL": 30,
      "sigs": [
        "PV",
        "BB"
      ],
      "flt": "none",
      "TP": 0.02,
      "SL": 0.05,
      "n": 17,
      "wr": 0.882,
      "pnl": 0.2285,
      "score": 20.165
    },
    "bear-calm": {
      "reason": "no_valid_combo"
    },
    "bear-storm": {
      "RH": 78,
      "RL": 30,
      "sigs": [
        "PV",
        "BB"
      ],
      "flt": "none",
      "TP": 0.006,
      "SL": 0.05,
      "n": 5,
      "wr": 1.0,
      "pnl": 0.0255,
      "score": 2.55
    },
    "chop": {
      "RH": 78,
      "RL": 30,
      "sigs": [
        "PV",
        "BB"
      ],
      "flt": "none",
      "TP": 0.012,
      "SL": 0.05,
      "n": 17,
      "wr": 0.941,
      "pnl": 0.1267,
      "score": 11.925
    }
  },
  "RESOLV": {
    "bull-calm": {
      "RH": 70,
      "RL": 30,
      "sigs": [
        "PV",
        "BB"
      ],
      "flt": "none",
      "TP": 0.02,
      "SL": 0.05,
      "n": 31,
      "wr": 0.742,
      "pnl": 0.0938,
      "score": 6.962
    },
    "bull-storm": {
      "RH": 78,
      "RL": 30,
      "sigs": [
        "BB"
      ],
      "flt": "none",
      "TP": 0.02,
      "SL": 0.05,
      "n": 7,
      "wr": 1.0,
      "pnl": 0.1337,
      "score": 13.37
    },
    "bear-calm": {
      "reason": "no_valid_combo"
    },
    "bear-storm": {
      "RH": 70,
      "RL": 30,
      "sigs": [
        "BB"
      ],
      "flt": "none",
      "TP": 0.006,
      "SL": 0.05,
      "n": 7,
      "wr": 1.0,
      "pnl": 0.0357,
      "score": 3.57
    },
    "chop": {
      "RH": 70,
      "RL": 30,
      "sigs": [
        "PV",
        "BB"
      ],
      "flt": "none",
      "TP": 0.02,
      "SL": 0.05,
      "n": 34,
      "wr": 0.794,
      "pnl": 0.1974,
      "score": 15.675
    }
  },
  "REZ": {
    "bull-calm": {
      "RH": 78,
      "RL": 22,
      "sigs": [
        "PV"
      ],
      "flt": "none",
      "TP": 0.012,
      "SL": 0.05,
      "n": 8,
      "wr": 1.0,
      "pnl": 0.0888,
      "score": 8.88
    },
    "bull-storm": {
      "RH": 78,
      "RL": 30,
      "sigs": [
        "PV",
        "BB"
      ],
      "flt": "none",
      "TP": 0.02,
      "SL": 0.05,
      "n": 15,
      "wr": 0.867,
      "pnl": 0.2184,
      "score": 18.931
    },
    "bear-calm": {
      "reason": "no_valid_combo"
    },
    "bear-storm": {
      "RH": 78,
      "RL": 30,
      "sigs": [
        "PV",
        "BB"
      ],
      "flt": "none",
      "TP": 0.02,
      "SL": 0.05,
      "n": 5,
      "wr": 1.0,
      "pnl": 0.0955,
      "score": 9.55
    },
    "chop": {
      "RH": 78,
      "RL": 30,
      "sigs": [
        "BB"
      ],
      "flt": "none",
      "TP": 0.02,
      "SL": 0.05,
      "n": 12,
      "wr": 0.833,
      "pnl": 0.0892,
      "score": 7.433
    }
  },
  "RUNE": {
    "bull-calm": {
      "RH": 70,
      "RL": 30,
      "sigs": [
        "PV",
        "BB"
      ],
      "flt": "none",
      "TP": 0.02,
      "SL": 0.05,
      "n": 23,
      "wr": 0.696,
      "pnl": 0.1852,
      "score": 12.884
    },
    "bull-storm": {
      "RH": 70,
      "RL": 30,
      "sigs": [
        "BB"
      ],
      "flt": "none",
      "TP": 0.012,
      "SL": 0.05,
      "n": 7,
      "wr": 0.857,
      "pnl": 0.0543,
      "score": 4.653
    },
    "bear-calm": {
      "reason": "no_valid_combo"
    },
    "bear-storm": {
      "RH": 78,
      "RL": 30,
      "sigs": [
        "PV",
        "BB"
      ],
      "flt": "none",
      "TP": 0.02,
      "SL": 0.05,
      "n": 5,
      "wr": 0.6,
      "pnl": 0.0327,
      "score": 1.96
    },
    "chop": {
      "RH": 78,
      "RL": 30,
      "sigs": [
        "PV",
        "BB"
      ],
      "flt": "none",
      "TP": 0.02,
      "SL": 0.05,
      "n": 20,
      "wr": 0.85,
      "pnl": 0.2478,
      "score": 21.06
    }
  },
  "SNX": {
    "bull-calm": {
      "RH": 78,
      "RL": 30,
      "sigs": [
        "PV",
        "BB"
      ],
      "flt": "none",
      "TP": 0.006,
      "SL": 0.05,
      "n": 17,
      "wr": 0.941,
      "pnl": 0.0393,
      "score": 3.699
    },
    "bull-storm": {
      "RH": 70,
      "RL": 30,
      "sigs": [
        "PV"
      ],
      "flt": "none",
      "TP": 0.012,
      "SL": 0.05,
      "n": 13,
      "wr": 0.923,
      "pnl": 0.1216,
      "score": 11.225
    },
    "bear-calm": {
      "reason": "no_valid_combo"
    },
    "bear-storm": {
      "RH": 70,
      "RL": 30,
      "sigs": [
        "PV",
        "BB"
      ],
      "flt": "none",
      "TP": 0.02,
      "SL": 0.05,
      "n": 6,
      "wr": 0.667,
      "pnl": 0.0465,
      "score": 3.097
    },
    "chop": {
      "RH": 78,
      "RL": 30,
      "sigs": [
        "PV",
        "BB"
      ],
      "flt": "none",
      "TP": 0.02,
      "SL": 0.05,
      "n": 20,
      "wr": 0.9,
      "pnl": 0.2635,
      "score": 23.715
    }
  },
  "STABLE": {
    "bull-calm": {
      "RH": 70,
      "RL": 30,
      "sigs": [
        "PV"
      ],
      "flt": "none",
      "TP": 0.02,
      "SL": 0.05,
      "n": 26,
      "wr": 0.923,
      "pnl": 0.3939,
      "score": 36.36
    },
    "bull-storm": {
      "RH": 70,
      "RL": 22,
      "sigs": [
        "PV"
      ],
      "flt": "none",
      "TP": 0.02,
      "SL": 0.05,
      "n": 7,
      "wr": 0.857,
      "pnl": 0.1041,
      "score": 8.926
    },
    "bear-calm": {
      "reason": "no_valid_combo"
    },
    "bear-storm": {
      "RH": 70,
      "RL": 30,
      "sigs": [
        "PV",
        "BB"
      ],
      "flt": "none",
      "TP": 0.02,
      "SL": 0.05,
      "n": 11,
      "wr": 0.818,
      "pnl": 0.0701,
      "score": 5.735
    },
    "chop": {
      "RH": 78,
      "RL": 30,
      "sigs": [
        "PV",
        "BB"
      ],
      "flt": "none",
      "TP": 0.02,
      "SL": 0.05,
      "n": 16,
      "wr": 0.875,
      "pnl": 0.1656,
      "score": 14.49
    }
  },
  "STBL": {
    "bull-calm": {
      "RH": 70,
      "RL": 30,
      "sigs": [
        "PV",
        "BB"
      ],
      "flt": "ema200",
      "TP": 0.02,
      "SL": 0.05,
      "n": 11,
      "wr": 1.0,
      "pnl": 0.2101,
      "score": 21.01
    },
    "bull-storm": {
      "RH": 78,
      "RL": 30,
      "sigs": [
        "BB"
      ],
      "flt": "none",
      "TP": 0.006,
      "SL": 0.05,
      "n": 9,
      "wr": 0.889,
      "pnl": -0.0101,
      "score": -0.898
    },
    "bear-calm": {
      "reason": "no_valid_combo"
    },
    "bear-storm": {
      "RH": 70,
      "RL": 30,
      "sigs": [
        "PV",
        "BB"
      ],
      "flt": "none",
      "TP": 0.02,
      "SL": 0.05,
      "n": 15,
      "wr": 0.867,
      "pnl": 0.1465,
      "score": 12.697
    },
    "chop": {
      "RH": 70,
      "RL": 30,
      "sigs": [
        "BB"
      ],
      "flt": "none",
      "TP": 0.02,
      "SL": 0.05,
      "n": 23,
      "wr": 0.913,
      "pnl": 0.2993,
      "score": 27.327
    }
  },
  "STX": {
    "bull-calm": {
      "RH": 70,
      "RL": 30,
      "sigs": [
        "PV",
        "BB"
      ],
      "flt": "none",
      "TP": 0.02,
      "SL": 0.05,
      "n": 21,
      "wr": 0.857,
      "pnl": 0.1911,
      "score": 16.38
    },
    "bull-storm": {
      "RH": 70,
      "RL": 22,
      "sigs": [
        "PV",
        "BB"
      ],
      "flt": "none",
      "TP": 0.02,
      "SL": 0.05,
      "n": 6,
      "wr": 1.0,
      "pnl": 0.1077,
      "score": 10.774
    },
    "bear-calm": {
      "reason": "no_valid_combo"
    },
    "bear-storm": {
      "RH": 70,
      "RL": 30,
      "sigs": [
        "PV",
        "BB"
      ],
      "flt": "none",
      "TP": 0.006,
      "SL": 0.05,
      "n": 5,
      "wr": 0.8,
      "pnl": -0.0305,
      "score": -2.44
    },
    "chop": {
      "RH": 78,
      "RL": 30,
      "sigs": [
        "PV"
      ],
      "flt": "none",
      "TP": 0.02,
      "SL": 0.05,
      "n": 21,
      "wr": 0.857,
      "pnl": 0.253,
      "score": 21.683
    }
  },
  "YZY": {
    "bull-calm": {
      "RH": 70,
      "RL": 30,
      "sigs": [
        "PV",
        "BB"
      ],
      "flt": "none",
      "TP": 0.02,
      "SL": 0.05,
      "n": 26,
      "wr": 0.808,
      "pnl": 0.3605,
      "score": 29.118
    },
    "bull-storm": {
      "RH": 78,
      "RL": 30,
      "sigs": [
        "PV"
      ],
      "flt": "ema200",
      "TP": 0.02,
      "SL": 0.05,
      "n": 9,
      "wr": 0.111,
      "pnl": -0.3881,
      "score": -4.312
    },
    "bear-calm": {
      "reason": "no_valid_combo"
    },
    "bear-storm": {
      "RH": 78,
      "RL": 30,
      "sigs": [
        "PV",
        "BB"
      ],
      "flt": "none",
      "TP": 0.02,
      "SL": 0.05,
      "n": 7,
      "wr": 0.857,
      "pnl": 0.1097,
      "score": 9.403
    },
    "chop": {
      "RH": 70,
      "RL": 22,
      "sigs": [
        "PV"
      ],
      "flt": "none",
      "TP": 0.02,
      "SL": 0.05,
      "n": 44,
      "wr": 0.614,
      "pnl": -0.3319,
      "score": -20.366
    }
  },
  "ZEC": {
    "bull-calm": {
      "RH": 70,
      "RL": 22,
      "sigs": [
        "PV",
        "BB"
      ],
      "flt": "none",
      "TP": 0.02,
      "SL": 0.05,
      "n": 16,
      "wr": 1.0,
      "pnl": 0.3056,
      "score": 30.56
    },
    "bull-storm": {
      "RH": 70,
      "RL": 30,
      "sigs": [
        "PV"
      ],
      "flt": "none",
      "TP": 0.02,
      "SL": 0.05,
      "n": 9,
      "wr": 1.0,
      "pnl": 0.1719,
      "score": 17.19
    },
    "bear-calm": {
      "reason": "no_valid_combo"
    },
    "bear-storm": {
      "RH": 78,
      "RL": 30,
      "sigs": [
        "PV",
        "BB"
      ],
      "flt": "none",
      "TP": 0.02,
      "SL": 0.05,
      "n": 5,
      "wr": 1.0,
      "pnl": 0.0955,
      "score": 9.55
    },
    "chop": {
      "RH": 78,
      "RL": 30,
      "sigs": [
        "PV",
        "BB"
      ],
      "flt": "none",
      "TP": 0.02,
      "SL": 0.05,
      "n": 27,
      "wr": 0.778,
      "pnl": 0.0957,
      "score": 7.443
    }
  },
  "kNEIRO": {
    "bull-calm": {
      "RH": 78,
      "RL": 30,
      "sigs": [
        "PV",
        "BB"
      ],
      "flt": "none",
      "TP": 0.02,
      "SL": 0.05,
      "n": 28,
      "wr": 0.75,
      "pnl": 0.0448,
      "score": 3.36
    },
    "bull-storm": {
      "RH": 70,
      "RL": 30,
      "sigs": [
        "PV",
        "BB"
      ],
      "flt": "none",
      "TP": 0.02,
      "SL": 0.05,
      "n": 25,
      "wr": 0.88,
      "pnl": 0.3121,
      "score": 27.464
    },
    "bear-calm": {
      "reason": "no_valid_combo"
    },
    "bear-storm": {
      "RH": 78,
      "RL": 30,
      "sigs": [
        "PV"
      ],
      "flt": "none",
      "TP": 0.006,
      "SL": 0.05,
      "n": 5,
      "wr": 0.8,
      "pnl": -0.0305,
      "score": -2.44
    },
    "chop": {
      "RH": 78,
      "RL": 30,
      "sigs": [
        "PV"
      ],
      "flt": "none",
      "TP": 0.02,
      "SL": 0.05,
      "n": 17,
      "wr": 0.765,
      "pnl": 0.0782,
      "score": 5.979
    }
  },
  "BABY": {
    "bull-calm": {
      "RH": 78,
      "RL": 30,
      "sigs": [
        "PV",
        "BB"
      ],
      "flt": "none",
      "TP": 0.02,
      "SL": 0.05,
      "n": 15,
      "wr": 0.933,
      "pnl": 0.2664,
      "score": 24.867
    },
    "bull-storm": {
      "RH": 70,
      "RL": 30,
      "sigs": [
        "BB"
      ],
      "flt": "none",
      "TP": 0.012,
      "SL": 0.05,
      "n": 6,
      "wr": 0.833,
      "pnl": 0.0046,
      "score": 0.383
    },
    "bear-calm": {
      "reason": "no_valid_combo"
    },
    "bear-storm": {
      "RH": 70,
      "RL": 30,
      "sigs": [
        "PV",
        "BB"
      ],
      "flt": "none",
      "TP": 0.012,
      "SL": 0.05,
      "n": 5,
      "wr": 1.0,
      "pnl": 0.0555,
      "score": 5.55
    },
    "chop": {
      "RH": 78,
      "RL": 30,
      "sigs": [
        "PV",
        "BB"
      ],
      "flt": "none",
      "TP": 0.02,
      "SL": 0.05,
      "n": 18,
      "wr": 1.0,
      "pnl": 0.3438,
      "score": 34.38
    }
  },
  "FOGO": {
    "bull-calm": {
      "RH": 78,
      "RL": 30,
      "sigs": [
        "PV"
      ],
      "flt": "none",
      "TP": 0.006,
      "SL": 0.05,
      "n": 13,
      "wr": 0.846,
      "pnl": -0.0457,
      "score": -3.867
    },
    "bull-storm": {
      "RH": 78,
      "RL": 30,
      "sigs": [
        "PV",
        "BB"
      ],
      "flt": "none",
      "TP": 0.012,
      "SL": 0.05,
      "n": 13,
      "wr": 0.769,
      "pnl": -0.0417,
      "score": -3.208
    },
    "bear-calm": {
      "reason": "no_valid_combo"
    },
    "bear-storm": {
      "RH": 70,
      "RL": 30,
      "sigs": [
        "PV",
        "BB"
      ],
      "flt": "none",
      "TP": 0.006,
      "SL": 0.05,
      "n": 9,
      "wr": 0.889,
      "pnl": -0.0101,
      "score": -0.898
    },
    "chop": {
      "RH": 78,
      "RL": 30,
      "sigs": [
        "PV",
        "BB"
      ],
      "flt": "none",
      "TP": 0.02,
      "SL": 0.05,
      "n": 11,
      "wr": 0.909,
      "pnl": 0.1401,
      "score": 12.736
    }
  },
  "MAVIA": {
    "bull-calm": {
      "RH": 70,
      "RL": 30,
      "sigs": [
        "PV",
        "BB"
      ],
      "flt": "none",
      "TP": 0.02,
      "SL": 0.05,
      "n": 12,
      "wr": 0.833,
      "pnl": 0.0892,
      "score": 7.433
    },
    "bull-storm": {
      "RH": 70,
      "RL": 22,
      "sigs": [
        "PV"
      ],
      "flt": "none",
      "TP": 0.02,
      "SL": 0.05,
      "n": 21,
      "wr": 0.857,
      "pnl": 0.1911,
      "score": 16.38
    },
    "bear-calm": {
      "reason": "no_valid_combo"
    },
    "bear-storm": {
      "RH": 78,
      "RL": 30,
      "sigs": [
        "PV",
        "BB"
      ],
      "flt": "none",
      "TP": 0.02,
      "SL": 0.05,
      "n": 5,
      "wr": 1.0,
      "pnl": 0.0955,
      "score": 9.55
    },
    "chop": {
      "RH": 70,
      "RL": 22,
      "sigs": [
        "BB"
      ],
      "flt": "none",
      "TP": 0.012,
      "SL": 0.05,
      "n": 5,
      "wr": 1.0,
      "pnl": 0.0555,
      "score": 5.55
    }
  },
  "PYTH": {
    "bull-calm": {
      "RH": 70,
      "RL": 22,
      "sigs": [
        "PV"
      ],
      "flt": "none",
      "TP": 0.02,
      "SL": 0.05,
      "n": 12,
      "wr": 1.0,
      "pnl": 0.2292,
      "score": 22.92
    },
    "bull-storm": {
      "RH": 70,
      "RL": 22,
      "sigs": [
        "PV",
        "BB"
      ],
      "flt": "none",
      "TP": 0.006,
      "SL": 0.05,
      "n": 6,
      "wr": 1.0,
      "pnl": 0.0306,
      "score": 3.06
    },
    "bear-calm": {
      "reason": "no_valid_combo"
    },
    "bear-storm": {
      "RH": 70,
      "RL": 30,
      "sigs": [
        "PV",
        "BB"
      ],
      "flt": "none",
      "TP": 0.02,
      "SL": 0.05,
      "n": 8,
      "wr": 0.875,
      "pnl": 0.1208,
      "score": 10.567
    },
    "chop": {
      "RH": 78,
      "RL": 30,
      "sigs": [
        "BB"
      ],
      "flt": "none",
      "TP": 0.02,
      "SL": 0.05,
      "n": 17,
      "wr": 0.824,
      "pnl": 0.1147,
      "score": 9.446
    }
  },
  "RSR": {
    "bull-calm": {
      "RH": 70,
      "RL": 30,
      "sigs": [
        "PV"
      ],
      "flt": "ema200",
      "TP": 0.006,
      "SL": 0.05,
      "n": 16,
      "wr": 1.0,
      "pnl": 0.0816,
      "score": 8.16
    },
    "bull-storm": {
      "RH": 70,
      "RL": 22,
      "sigs": [
        "PV",
        "BB"
      ],
      "flt": "none",
      "TP": 0.02,
      "SL": 0.05,
      "n": 18,
      "wr": 0.833,
      "pnl": 0.2098,
      "score": 17.484
    },
    "bear-calm": {
      "reason": "insufficient_bars_41",
      "n": 0
    },
    "bear-storm": {
      "RH": 78,
      "RL": 30,
      "sigs": [
        "PV"
      ],
      "flt": "none",
      "TP": 0.02,
      "SL": 0.05,
      "n": 13,
      "wr": 0.846,
      "pnl": 0.1083,
      "score": 9.164
    },
    "chop": {
      "RH": 78,
      "RL": 30,
      "sigs": [
        "PV"
      ],
      "flt": "none",
      "TP": 0.012,
      "SL": 0.05,
      "n": 46,
      "wr": 0.848,
      "pnl": 0.1191,
      "score": 10.098
    }
  },
  "TRUMP": {
    "bull-calm": {
      "RH": 70,
      "RL": 22,
      "sigs": [
        "PV",
        "BB"
      ],
      "flt": "none",
      "TP": 0.02,
      "SL": 0.05,
      "n": 11,
      "wr": 0.818,
      "pnl": 0.0805,
      "score": 6.587
    },
    "bull-storm": {
      "RH": 70,
      "RL": 30,
      "sigs": [
        "PV"
      ],
      "flt": "none",
      "TP": 0.012,
      "SL": 0.05,
      "n": 14,
      "wr": 0.857,
      "pnl": 0.0964,
      "score": 8.262
    },
    "bear-calm": {
      "reason": "no_valid_combo"
    },
    "bear-storm": {
      "reason": "no_valid_combo"
    },
    "chop": {
      "RH": 78,
      "RL": 30,
      "sigs": [
        "BB"
      ],
      "flt": "none",
      "TP": 0.02,
      "SL": 0.05,
      "n": 7,
      "wr": 0.857,
      "pnl": 0.0637,
      "score": 5.46
    }
  },
  "VINE": {
    "bull-calm": {
      "RH": 70,
      "RL": 22,
      "sigs": [
        "BB"
      ],
      "flt": "none",
      "TP": 0.012,
      "SL": 0.05,
      "n": 12,
      "wr": 0.917,
      "pnl": 0.0712,
      "score": 6.527
    },
    "bull-storm": {
      "RH": 70,
      "RL": 22,
      "sigs": [
        "PV"
      ],
      "flt": "none",
      "TP": 0.02,
      "SL": 0.05,
      "n": 9,
      "wr": 1.0,
      "pnl": 0.1719,
      "score": 17.19
    },
    "bear-calm": {
      "reason": "no_valid_combo"
    },
    "bear-storm": {
      "RH": 78,
      "RL": 30,
      "sigs": [
        "PV"
      ],
      "flt": "none",
      "TP": 0.02,
      "SL": 0.05,
      "n": 6,
      "wr": 1.0,
      "pnl": 0.1146,
      "score": 11.46
    },
    "chop": {
      "RH": 78,
      "RL": 30,
      "sigs": [
        "PV",
        "BB"
      ],
      "flt": "none",
      "TP": 0.012,
      "SL": 0.05,
      "n": 18,
      "wr": 0.944,
      "pnl": 0.1378,
      "score": 13.014
    }
  },
  "XLM": {
    "bull-calm": {
      "RH": 78,
      "RL": 30,
      "sigs": [
        "PV",
        "BB"
      ],
      "flt": "none",
      "TP": 0.02,
      "SL": 0.05,
      "n": 19,
      "wr": 0.737,
      "pnl": 0.1532,
      "score": 11.286
    },
    "bull-storm": {
      "RH": 70,
      "RL": 22,
      "sigs": [
        "PV"
      ],
      "flt": "none",
      "TP": 0.02,
      "SL": 0.05,
      "n": 6,
      "wr": 1.0,
      "pnl": 0.1022,
      "score": 10.219
    },
    "bear-calm": {
      "reason": "no_valid_combo"
    },
    "bear-storm": {
      "RH": 78,
      "RL": 30,
      "sigs": [
        "PV",
        "BB"
      ],
      "flt": "none",
      "TP": 0.006,
      "SL": 0.05,
      "n": 5,
      "wr": 1.0,
      "pnl": 0.0255,
      "score": 2.55
    },
    "chop": {
      "RH": 78,
      "RL": 30,
      "sigs": [
        "PV",
        "BB"
      ],
      "flt": "none",
      "TP": 0.012,
      "SL": 0.05,
      "n": 24,
      "wr": 1.0,
      "pnl": 0.2664,
      "score": 26.64
    }
  },
  "BLUR": {
    "bull-calm": {
      "RH": 70,
      "RL": 30,
      "sigs": [
        "PV"
      ],
      "flt": "none",
      "TP": 0.02,
      "SL": 0.05,
      "n": 25,
      "wr": 0.88,
      "pnl": 0.2525,
      "score": 22.218
    },
    "bull-storm": {
      "RH": 78,
      "RL": 30,
      "sigs": [
        "BB"
      ],
      "flt": "none",
      "TP": 0.02,
      "SL": 0.05,
      "n": 6,
      "wr": 1.0,
      "pnl": 0.1146,
      "score": 11.46
    },
    "bear-calm": {
      "RH": 78,
      "RL": 30,
      "sigs": [
        "BB"
      ],
      "flt": "none",
      "TP": 0.006,
      "SL": 0.05,
      "n": 6,
      "wr": 1.0,
      "pnl": 0.0306,
      "score": 3.06
    },
    "bear-storm": {
      "RH": 70,
      "RL": 30,
      "sigs": [
        "PV"
      ],
      "flt": "none",
      "TP": 0.012,
      "SL": 0.05,
      "n": 9,
      "wr": 0.889,
      "pnl": 0.0379,
      "score": 3.369
    },
    "chop": {
      "RH": 78,
      "RL": 22,
      "sigs": [
        "PV",
        "BB"
      ],
      "flt": "none",
      "TP": 0.006,
      "SL": 0.05,
      "n": 10,
      "wr": 1.0,
      "pnl": 0.051,
      "score": 5.1
    }
  },
  "BNB": {
    "bull-calm": {
      "RH": 70,
      "RL": 30,
      "sigs": [
        "BB"
      ],
      "flt": "none",
      "TP": 0.012,
      "SL": 0.05,
      "n": 16,
      "wr": 0.812,
      "pnl": 0.0735,
      "score": 5.975
    },
    "bull-storm": {
      "RH": 70,
      "RL": 30,
      "sigs": [
        "PV",
        "BB"
      ],
      "flt": "none",
      "TP": 0.012,
      "SL": 0.05,
      "n": 17,
      "wr": 0.941,
      "pnl": 0.1484,
      "score": 13.966
    },
    "bear-calm": {
      "RH": 78,
      "RL": 30,
      "sigs": [
        "PV"
      ],
      "flt": "none",
      "TP": 0.02,
      "SL": 0.05,
      "n": 7,
      "wr": 0.857,
      "pnl": 0.0834,
      "score": 7.145
    },
    "bear-storm": {
      "RH": 70,
      "RL": 22,
      "sigs": [
        "PV"
      ],
      "flt": "none",
      "TP": 0.012,
      "SL": 0.05,
      "n": 6,
      "wr": 0.0,
      "pnl": -0.1498,
      "score": -0.0
    },
    "chop": {
      "RH": 70,
      "RL": 30,
      "sigs": [
        "BB"
      ],
      "flt": "none",
      "TP": 0.012,
      "SL": 0.05,
      "n": 28,
      "wr": 0.893,
      "pnl": 0.206,
      "score": 18.389
    }
  },
  "BTC": {
    "bull-calm": {
      "RH": 70,
      "RL": 22,
      "sigs": [
        "BB"
      ],
      "flt": "none",
      "TP": 0.02,
      "SL": 0.05,
      "n": 6,
      "wr": 0.833,
      "pnl": 0.0733,
      "score": 6.105
    },
    "bull-storm": {
      "RH": 70,
      "RL": 30,
      "sigs": [
        "PV",
        "BB"
      ],
      "flt": "none",
      "TP": 0.012,
      "SL": 0.05,
      "n": 12,
      "wr": 1.0,
      "pnl": 0.1243,
      "score": 12.43
    },
    "bear-calm": {
      "reason": "no_valid_combo"
    },
    "bear-storm": {
      "RH": 78,
      "RL": 30,
      "sigs": [
        "PV",
        "BB"
      ],
      "flt": "none",
      "TP": 0.02,
      "SL": 0.05,
      "n": 6,
      "wr": 0.833,
      "pnl": 0.0791,
      "score": 6.592
    },
    "chop": {
      "RH": 78,
      "RL": 30,
      "sigs": [
        "PV"
      ],
      "flt": "none",
      "TP": 0.02,
      "SL": 0.05,
      "n": 19,
      "wr": 0.737,
      "pnl": 0.1071,
      "score": 7.894
    }
  },
  "JUP": {
    "bull-calm": {
      "RH": 70,
      "RL": 22,
      "sigs": [
        "PV",
        "BB"
      ],
      "flt": "none",
      "TP": 0.012,
      "SL": 0.05,
      "n": 17,
      "wr": 0.941,
      "pnl": 0.1267,
      "score": 11.925
    },
    "bull-storm": {
      "RH": 70,
      "RL": 22,
      "sigs": [
        "PV",
        "BB"
      ],
      "flt": "none",
      "TP": 0.02,
      "SL": 0.05,
      "n": 11,
      "wr": 1.0,
      "pnl": 0.2101,
      "score": 21.01
    },
    "bear-calm": {
      "RH": 70,
      "RL": 22,
      "sigs": [
        "PV",
        "BB"
      ],
      "flt": "none",
      "TP": 0.02,
      "SL": 0.05,
      "n": 6,
      "wr": 0.833,
      "pnl": 0.0598,
      "score": 4.984
    },
    "bear-storm": {
      "RH": 70,
      "RL": 30,
      "sigs": [
        "PV",
        "BB"
      ],
      "flt": "none",
      "TP": 0.006,
      "SL": 0.05,
      "n": 9,
      "wr": 1.0,
      "pnl": 0.0459,
      "score": 4.59
    },
    "chop": {
      "RH": 78,
      "RL": 30,
      "sigs": [
        "PV",
        "BB"
      ],
      "flt": "none",
      "TP": 0.02,
      "SL": 0.05,
      "n": 23,
      "wr": 0.913,
      "pnl": 0.2993,
      "score": 27.327
    }
  },
  "LTC": {
    "bull-calm": {
      "RH": 78,
      "RL": 30,
      "sigs": [
        "PV"
      ],
      "flt": "none",
      "TP": 0.006,
      "SL": 0.05,
      "n": 10,
      "wr": 1.0,
      "pnl": 0.051,
      "score": 5.1
    },
    "bull-storm": {
      "RH": 70,
      "RL": 30,
      "sigs": [
        "PV"
      ],
      "flt": "none",
      "TP": 0.02,
      "SL": 0.05,
      "n": 17,
      "wr": 0.706,
      "pnl": 0.137,
      "score": 9.669
    },
    "bear-calm": {
      "RH": 70,
      "RL": 30,
      "sigs": [
        "PV",
        "BB"
      ],
      "flt": "none",
      "TP": 0.02,
      "SL": 0.05,
      "n": 12,
      "wr": 0.667,
      "pnl": 0.0567,
      "score": 3.782
    },
    "bear-storm": {
      "RH": 70,
      "RL": 30,
      "sigs": [
        "PV",
        "BB"
      ],
      "flt": "none",
      "TP": 0.02,
      "SL": 0.05,
      "n": 8,
      "wr": 0.875,
      "pnl": 0.1021,
      "score": 8.934
    },
    "chop": {
      "RH": 78,
      "RL": 30,
      "sigs": [
        "PV",
        "BB"
      ],
      "flt": "none",
      "TP": 0.02,
      "SL": 0.05,
      "n": 28,
      "wr": 0.821,
      "pnl": 0.2986,
      "score": 24.532
    }
  },
  "MAV": {
    "bull-calm": {
      "RH": 78,
      "RL": 22,
      "sigs": [
        "PV"
      ],
      "flt": "none",
      "TP": 0.02,
      "SL": 0.05,
      "n": 9,
      "wr": 0.889,
      "pnl": 0.1019,
      "score": 9.058
    },
    "bull-storm": {
      "RH": 70,
      "RL": 30,
      "sigs": [
        "PV",
        "BB"
      ],
      "flt": "none",
      "TP": 0.02,
      "SL": 0.05,
      "n": 13,
      "wr": 0.846,
      "pnl": 0.1083,
      "score": 9.164
    },
    "bear-calm": {
      "reason": "no_valid_combo"
    },
    "bear-storm": {
      "RH": 78,
      "RL": 30,
      "sigs": [
        "PV"
      ],
      "flt": "none",
      "TP": 0.02,
      "SL": 0.05,
      "n": 7,
      "wr": 1.0,
      "pnl": 0.1337,
      "score": 13.37
    },
    "chop": {
      "RH": 70,
      "RL": 30,
      "sigs": [
        "BB"
      ],
      "flt": "none",
      "TP": 0.006,
      "SL": 0.05,
      "n": 11,
      "wr": 0.909,
      "pnl": 0.0054,
      "score": 0.488
    }
  },
  "MEW": {
    "bull-calm": {
      "RH": 78,
      "RL": 22,
      "sigs": [
        "PV",
        "BB"
      ],
      "flt": "none",
      "TP": 0.02,
      "SL": 0.05,
      "n": 40,
      "wr": 0.7,
      "pnl": 0.0882,
      "score": 6.174
    },
    "bull-storm": {
      "RH": 70,
      "RL": 22,
      "sigs": [
        "PV",
        "BB"
      ],
      "flt": "none",
      "TP": 0.02,
      "SL": 0.05,
      "n": 27,
      "wr": 0.852,
      "pnl": 0.264,
      "score": 22.485
    },
    "bear-calm": {
      "reason": "insufficient_bars_48",
      "n": 0
    },
    "bear-storm": {
      "RH": 70,
      "RL": 30,
      "sigs": [
        "PV"
      ],
      "flt": "none",
      "TP": 0.02,
      "SL": 0.05,
      "n": 15,
      "wr": 0.933,
      "pnl": 0.2165,
      "score": 20.207
    },
    "chop": {
      "RH": 78,
      "RL": 30,
      "sigs": [
        "PV"
      ],
      "flt": "none",
      "TP": 0.02,
      "SL": 0.05,
      "n": 62,
      "wr": 0.758,
      "pnl": 0.165,
      "score": 12.506
    }
  },
  "SAND": {
    "bull-calm": {
      "RH": 70,
      "RL": 30,
      "sigs": [
        "PV"
      ],
      "flt": "none",
      "TP": 0.02,
      "SL": 0.05,
      "n": 25,
      "wr": 0.84,
      "pnl": 0.225,
      "score": 18.902
    },
    "bull-storm": {
      "RH": 70,
      "RL": 30,
      "sigs": [
        "PV",
        "BB"
      ],
      "flt": "none",
      "TP": 0.02,
      "SL": 0.05,
      "n": 19,
      "wr": 0.842,
      "pnl": 0.2148,
      "score": 18.084
    },
    "bear-calm": {
      "RH": 78,
      "RL": 30,
      "sigs": [
        "PV"
      ],
      "flt": "none",
      "TP": 0.02,
      "SL": 0.05,
      "n": 6,
      "wr": 1.0,
      "pnl": 0.1102,
      "score": 11.024
    },
    "bear-storm": {
      "RH": 70,
      "RL": 30,
      "sigs": [
        "PV",
        "BB"
      ],
      "flt": "none",
      "TP": 0.006,
      "SL": 0.05,
      "n": 7,
      "wr": 0.857,
      "pnl": -0.0203,
      "score": -1.74
    },
    "chop": {
      "RH": 78,
      "RL": 30,
      "sigs": [
        "PV",
        "BB"
      ],
      "flt": "none",
      "TP": 0.02,
      "SL": 0.05,
      "n": 30,
      "wr": 0.833,
      "pnl": 0.2779,
      "score": 23.16
    }
  },
  "SUSHI": {
    "bull-calm": {
      "RH": 78,
      "RL": 22,
      "sigs": [
        "PV"
      ],
      "flt": "none",
      "TP": 0.012,
      "SL": 0.05,
      "n": 7,
      "wr": 1.0,
      "pnl": 0.0777,
      "score": 7.77
    },
    "bull-storm": {
      "RH": 70,
      "RL": 22,
      "sigs": [
        "PV"
      ],
      "flt": "none",
      "TP": 0.02,
      "SL": 0.05,
      "n": 6,
      "wr": 1.0,
      "pnl": 0.108,
      "score": 10.799
    },
    "bear-calm": {
      "RH": 78,
      "RL": 30,
      "sigs": [
        "PV"
      ],
      "flt": "none",
      "TP": 0.02,
      "SL": 0.05,
      "n": 5,
      "wr": 1.0,
      "pnl": 0.0955,
      "score": 9.55
    },
    "bear-storm": {
      "reason": "no_valid_combo"
    },
    "chop": {
      "RH": 78,
      "RL": 30,
      "sigs": [
        "PV",
        "BB"
      ],
      "flt": "none",
      "TP": 0.02,
      "SL": 0.05,
      "n": 21,
      "wr": 0.952,
      "pnl": 0.3311,
      "score": 31.533
    }
  },
  "TAO": {
    "bull-calm": {
      "RH": 70,
      "RL": 22,
      "sigs": [
        "PV",
        "BB"
      ],
      "flt": "none",
      "TP": 0.02,
      "SL": 0.05,
      "n": 12,
      "wr": 1.0,
      "pnl": 0.2292,
      "score": 22.92
    },
    "bull-storm": {
      "RH": 78,
      "RL": 30,
      "sigs": [
        "PV",
        "BB"
      ],
      "flt": "none",
      "TP": 0.02,
      "SL": 0.05,
      "n": 14,
      "wr": 0.857,
      "pnl": 0.1274,
      "score": 10.92
    },
    "bear-calm": {
      "RH": 70,
      "RL": 30,
      "sigs": [
        "PV",
        "BB"
      ],
      "flt": "none",
      "TP": 0.02,
      "SL": 0.05,
      "n": 12,
      "wr": 0.833,
      "pnl": 0.154,
      "score": 12.837
    },
    "bear-storm": {
      "RH": 70,
      "RL": 30,
      "sigs": [
        "PV"
      ],
      "flt": "none",
      "TP": 0.02,
      "SL": 0.05,
      "n": 5,
      "wr": 1.0,
      "pnl": 0.0955,
      "score": 9.55
    },
    "chop": {
      "RH": 70,
      "RL": 30,
      "sigs": [
        "PV",
        "BB"
      ],
      "flt": "ema200",
      "TP": 0.02,
      "SL": 0.05,
      "n": 6,
      "wr": 1.0,
      "pnl": 0.1146,
      "score": 11.46
    }
  },
  "kBONK": {
    "bull-calm": {
      "RH": 70,
      "RL": 22,
      "sigs": [
        "PV",
        "BB"
      ],
      "flt": "none",
      "TP": 0.02,
      "SL": 0.05,
      "n": 12,
      "wr": 0.917,
      "pnl": 0.1592,
      "score": 14.593
    },
    "bull-storm": {
      "RH": 70,
      "RL": 30,
      "sigs": [
        "BB"
      ],
      "flt": "none",
      "TP": 0.02,
      "SL": 0.05,
      "n": 11,
      "wr": 0.909,
      "pnl": 0.1401,
      "score": 12.736
    },
    "bear-calm": {
      "RH": 78,
      "RL": 30,
      "sigs": [
        "PV",
        "BB"
      ],
      "flt": "none",
      "TP": 0.02,
      "SL": 0.05,
      "n": 9,
      "wr": 0.889,
      "pnl": 0.1452,
      "score": 12.905
    },
    "bear-storm": {
      "RH": 70,
      "RL": 30,
      "sigs": [
        "PV"
      ],
      "flt": "none",
      "TP": 0.02,
      "SL": 0.05,
      "n": 6,
      "wr": 0.667,
      "pnl": -0.0254,
      "score": -1.693
    },
    "chop": {
      "RH": 70,
      "RL": 30,
      "sigs": [
        "BB"
      ],
      "flt": "none",
      "TP": 0.02,
      "SL": 0.05,
      "n": 26,
      "wr": 0.846,
      "pnl": 0.2396,
      "score": 20.27
    }
  },
  "HMSTR": {
    "bull-calm": {
      "RH": 78,
      "RL": 22,
      "sigs": [
        "PV"
      ],
      "flt": "none",
      "TP": 0.02,
      "SL": 0.05,
      "n": 21,
      "wr": 1.0,
      "pnl": 0.4011,
      "score": 40.11
    },
    "bull-storm": {
      "RH": 70,
      "RL": 30,
      "sigs": [
        "PV",
        "BB"
      ],
      "flt": "none",
      "TP": 0.02,
      "SL": 0.05,
      "n": 48,
      "wr": 0.875,
      "pnl": 0.52,
      "score": 45.5
    },
    "bear-calm": {
      "reason": "insufficient_bars_49",
      "n": 0
    },
    "bear-storm": {
      "RH": 70,
      "RL": 30,
      "sigs": [
        "PV"
      ],
      "flt": "none",
      "TP": 0.02,
      "SL": 0.05,
      "n": 15,
      "wr": 0.8,
      "pnl": 0.0765,
      "score": 6.12
    },
    "chop": {
      "RH": 78,
      "RL": 30,
      "sigs": [
        "PV"
      ],
      "flt": "none",
      "TP": 0.02,
      "SL": 0.05,
      "n": 61,
      "wr": 0.77,
      "pnl": 0.2498,
      "score": 19.25
    }
  },
  "NOT": {
    "bull-calm": {
      "RH": 78,
      "RL": 22,
      "sigs": [
        "PV",
        "BB"
      ],
      "flt": "none",
      "TP": 0.012,
      "SL": 0.05,
      "n": 34,
      "wr": 0.912,
      "pnl": 0.1939,
      "score": 17.679
    },
    "bull-storm": {
      "RH": 70,
      "RL": 22,
      "sigs": [
        "PV"
      ],
      "flt": "ema200",
      "TP": 0.02,
      "SL": 0.05,
      "n": 8,
      "wr": 0.25,
      "pnl": -0.2672,
      "score": -6.68
    },
    "bear-calm": {
      "reason": "insufficient_bars_39",
      "n": 0
    },
    "bear-storm": {
      "RH": 78,
      "RL": 30,
      "sigs": [
        "PV"
      ],
      "flt": "none",
      "TP": 0.02,
      "SL": 0.05,
      "n": 19,
      "wr": 0.842,
      "pnl": 0.1529,
      "score": 12.876
    },
    "chop": {
      "RH": 78,
      "RL": 30,
      "sigs": [
        "PV"
      ],
      "flt": "ema200",
      "TP": 0.02,
      "SL": 0.05,
      "n": 19,
      "wr": 0.737,
      "pnl": 0.0129,
      "score": 0.951
    }
  },
  "ARB": {
    "bull-calm": {
      "RH": 70,
      "RL": 30,
      "sigs": [
        "PV",
        "BB"
      ],
      "flt": "none",
      "TP": 0.02,
      "SL": 0.05,
      "n": 26,
      "wr": 0.808,
      "pnl": 0.2628,
      "score": 21.228
    },
    "bull-storm": {
      "RH": 78,
      "RL": 30,
      "sigs": [
        "PV"
      ],
      "flt": "none",
      "TP": 0.02,
      "SL": 0.05,
      "n": 11,
      "wr": 0.909,
      "pnl": 0.1794,
      "score": 16.311
    },
    "bear-calm": {
      "reason": "no_valid_combo"
    },
    "bear-storm": {
      "reason": "no_valid_combo"
    },
    "chop": {
      "RH": 78,
      "RL": 30,
      "sigs": [
        "PV",
        "BB"
      ],
      "flt": "ema200",
      "TP": 0.012,
      "SL": 0.05,
      "n": 6,
      "wr": 1.0,
      "pnl": 0.0666,
      "score": 6.66
    }
  },
  "ARK": {
    "bull-calm": {
      "RH": 70,
      "RL": 30,
      "sigs": [
        "PV",
        "BB"
      ],
      "flt": "none",
      "TP": 0.012,
      "SL": 0.05,
      "n": 28,
      "wr": 0.786,
      "pnl": 0.1129,
      "score": 8.871
    },
    "bull-storm": {
      "RH": 70,
      "RL": 22,
      "sigs": [
        "PV"
      ],
      "flt": "none",
      "TP": 0.02,
      "SL": 0.05,
      "n": 10,
      "wr": 0.8,
      "pnl": 0.1201,
      "score": 9.609
    },
    "bear-calm": {
      "reason": "no_valid_combo"
    },
    "bear-storm": {
      "RH": 70,
      "RL": 22,
      "sigs": [
        "PV"
      ],
      "flt": "none",
      "TP": 0.02,
      "SL": 0.05,
      "n": 6,
      "wr": 0.0,
      "pnl": -0.1494,
      "score": -0.0
    },
    "chop": {
      "RH": 78,
      "RL": 30,
      "sigs": [
        "PV",
        "BB"
      ],
      "flt": "none",
      "TP": 0.02,
      "SL": 0.05,
      "n": 19,
      "wr": 0.789,
      "pnl": 0.2088,
      "score": 16.487
    }
  },
  "BANANA": {
    "bull-calm": {
      "RH": 78,
      "RL": 30,
      "sigs": [
        "PV"
      ],
      "flt": "none",
      "TP": 0.02,
      "SL": 0.05,
      "n": 18,
      "wr": 0.889,
      "pnl": 0.1917,
      "score": 17.039
    },
    "bull-storm": {
      "RH": 70,
      "RL": 30,
      "sigs": [
        "PV",
        "BB"
      ],
      "flt": "none",
      "TP": 0.02,
      "SL": 0.05,
      "n": 20,
      "wr": 0.8,
      "pnl": 0.1699,
      "score": 13.591
    },
    "bear-calm": {
      "reason": "no_valid_combo"
    },
    "bear-storm": {
      "RH": 78,
      "RL": 30,
      "sigs": [
        "PV",
        "BB"
      ],
      "flt": "none",
      "TP": 0.02,
      "SL": 0.05,
      "n": 7,
      "wr": 0.857,
      "pnl": 0.1049,
      "score": 8.991
    },
    "chop": {
      "RH": 78,
      "RL": 30,
      "sigs": [
        "PV",
        "BB"
      ],
      "flt": "none",
      "TP": 0.02,
      "SL": 0.05,
      "n": 18,
      "wr": 0.833,
      "pnl": 0.1219,
      "score": 10.154
    }
  },
  "BIGTIME": {
    "bull-calm": {
      "RH": 78,
      "RL": 30,
      "sigs": [
        "BB"
      ],
      "flt": "none",
      "TP": 0.02,
      "SL": 0.05,
      "n": 10,
      "wr": 0.9,
      "pnl": 0.121,
      "score": 10.89
    },
    "bull-storm": {
      "RH": 70,
      "RL": 22,
      "sigs": [
        "PV",
        "BB"
      ],
      "flt": "none",
      "TP": 0.02,
      "SL": 0.05,
      "n": 11,
      "wr": 0.909,
      "pnl": 0.1622,
      "score": 14.747
    },
    "bear-calm": {
      "reason": "no_valid_combo"
    },
    "bear-storm": {
      "RH": 70,
      "RL": 30,
      "sigs": [
        "BB"
      ],
      "flt": "none",
      "TP": 0.006,
      "SL": 0.05,
      "n": 5,
      "wr": 1.0,
      "pnl": 0.0255,
      "score": 2.55
    },
    "chop": {
      "RH": 78,
      "RL": 30,
      "sigs": [
        "PV",
        "BB"
      ],
      "flt": "none",
      "TP": 0.012,
      "SL": 0.05,
      "n": 17,
      "wr": 1.0,
      "pnl": 0.1887,
      "score": 18.87
    }
  },
  "BLAST": {
    "bull-calm": {
      "RH": 70,
      "RL": 30,
      "sigs": [
        "PV"
      ],
      "flt": "ema200",
      "TP": 0.02,
      "SL": 0.05,
      "n": 13,
      "wr": 0.846,
      "pnl": 0.1083,
      "score": 9.164
    },
    "bull-storm": {
      "RH": 70,
      "RL": 30,
      "sigs": [
        "PV",
        "BB"
      ],
      "flt": "ema200",
      "TP": 0.02,
      "SL": 0.05,
      "n": 6,
      "wr": 1.0,
      "pnl": 0.1146,
      "score": 11.46
    },
    "bear-calm": {
      "reason": "insufficient_bars_48",
      "n": 0
    },
    "bear-storm": {
      "RH": 78,
      "RL": 30,
      "sigs": [
        "PV"
      ],
      "flt": "none",
      "TP": 0.02,
      "SL": 0.05,
      "n": 10,
      "wr": 1.0,
      "pnl": 0.191,
      "score": 19.1
    },
    "chop": {
      "RH": 78,
      "RL": 30,
      "sigs": [
        "PV"
      ],
      "flt": "ema200",
      "TP": 0.02,
      "SL": 0.05,
      "n": 12,
      "wr": 0.917,
      "pnl": 0.1592,
      "score": 14.593
    }
  },
  "BSV": {
    "bull-calm": {
      "RH": 70,
      "RL": 22,
      "sigs": [
        "PV",
        "BB"
      ],
      "flt": "none",
      "TP": 0.012,
      "SL": 0.05,
      "n": 12,
      "wr": 0.917,
      "pnl": 0.122,
      "score": 11.187
    },
    "bull-storm": {
      "RH": 70,
      "RL": 30,
      "sigs": [
        "PV",
        "BB"
      ],
      "flt": "none",
      "TP": 0.02,
      "SL": 0.05,
      "n": 18,
      "wr": 0.722,
      "pnl": 0.0893,
      "score": 6.45
    },
    "bear-calm": {
      "reason": "no_valid_combo"
    },
    "bear-storm": {
      "RH": 70,
      "RL": 30,
      "sigs": [
        "PV"
      ],
      "flt": "none",
      "TP": 0.006,
      "SL": 0.05,
      "n": 6,
      "wr": 1.0,
      "pnl": 0.0306,
      "score": 3.06
    },
    "chop": {
      "RH": 78,
      "RL": 30,
      "sigs": [
        "PV"
      ],
      "flt": "none",
      "TP": 0.02,
      "SL": 0.05,
      "n": 16,
      "wr": 1.0,
      "pnl": 0.3056,
      "score": 30.56
    }
  },
  "CAKE": {
    "bull-calm": {
      "RH": 70,
      "RL": 30,
      "sigs": [
        "BB"
      ],
      "flt": "none",
      "TP": 0.02,
      "SL": 0.05,
      "n": 15,
      "wr": 0.8,
      "pnl": 0.1152,
      "score": 9.213
    },
    "bull-storm": {
      "RH": 70,
      "RL": 22,
      "sigs": [
        "PV",
        "BB"
      ],
      "flt": "none",
      "TP": 0.02,
      "SL": 0.05,
      "n": 6,
      "wr": 0.833,
      "pnl": 0.075,
      "score": 6.252
    },
    "bear-calm": {
      "reason": "no_valid_combo"
    },
    "bear-storm": {
      "RH": 70,
      "RL": 22,
      "sigs": [
        "BB"
      ],
      "flt": "none",
      "TP": 0.012,
      "SL": 0.05,
      "n": 5,
      "wr": 0.0,
      "pnl": -0.2545,
      "score": -0.0
    },
    "chop": {
      "RH": 78,
      "RL": 30,
      "sigs": [
        "PV",
        "BB"
      ],
      "flt": "none",
      "TP": 0.02,
      "SL": 0.05,
      "n": 16,
      "wr": 0.812,
      "pnl": 0.0956,
      "score": 7.767
    }
  },
  "CRV": {
    "bull-calm": {
      "RH": 78,
      "RL": 22,
      "sigs": [
        "PV"
      ],
      "flt": "none",
      "TP": 0.02,
      "SL": 0.05,
      "n": 6,
      "wr": 0.833,
      "pnl": 0.0446,
      "score": 3.717
    },
    "bull-storm": {
      "RH": 70,
      "RL": 30,
      "sigs": [
        "PV"
      ],
      "flt": "none",
      "TP": 0.012,
      "SL": 0.05,
      "n": 18,
      "wr": 0.944,
      "pnl": 0.1688,
      "score": 15.943
    },
    "bear-calm": {
      "reason": "no_valid_combo"
    },
    "bear-storm": {
      "RH": 70,
      "RL": 30,
      "sigs": [
        "PV",
        "BB"
      ],
      "flt": "none",
      "TP": 0.006,
      "SL": 0.05,
      "n": 10,
      "wr": 1.0,
      "pnl": 0.051,
      "score": 5.1
    },
    "chop": {
      "RH": 78,
      "RL": 30,
      "sigs": [
        "PV",
        "BB"
      ],
      "flt": "none",
      "TP": 0.02,
      "SL": 0.05,
      "n": 31,
      "wr": 0.839,
      "pnl": 0.3348,
      "score": 28.077
    }
  },
  "DOOD": {
    "bull-calm": {
      "RH": 70,
      "RL": 30,
      "sigs": [
        "PV",
        "BB"
      ],
      "flt": "none",
      "TP": 0.02,
      "SL": 0.05,
      "n": 27,
      "wr": 0.889,
      "pnl": 0.3057,
      "score": 27.173
    },
    "bull-storm": {
      "RH": 70,
      "RL": 22,
      "sigs": [
        "PV"
      ],
      "flt": "none",
      "TP": 0.02,
      "SL": 0.05,
      "n": 9,
      "wr": 1.0,
      "pnl": 0.1719,
      "score": 17.19
    },
    "bear-calm": {
      "reason": "no_valid_combo"
    },
    "bear-storm": {
      "RH": 78,
      "RL": 30,
      "sigs": [
        "PV"
      ],
      "flt": "none",
      "TP": 0.012,
      "SL": 0.05,
      "n": 5,
      "wr": 1.0,
      "pnl": 0.0555,
      "score": 5.55
    },
    "chop": {
      "RH": 78,
      "RL": 30,
      "sigs": [
        "PV",
        "BB"
      ],
      "flt": "none",
      "TP": 0.012,
      "SL": 0.05,
      "n": 20,
      "wr": 0.85,
      "pnl": 0.0694,
      "score": 5.896
    }
  },
  "ETHFI": {
    "bull-calm": {
      "RH": 70,
      "RL": 30,
      "sigs": [
        "BB"
      ],
      "flt": "none",
      "TP": 0.02,
      "SL": 0.05,
      "n": 14,
      "wr": 0.786,
      "pnl": 0.0574,
      "score": 4.51
    },
    "bull-storm": {
      "RH": 70,
      "RL": 22,
      "sigs": [
        "PV"
      ],
      "flt": "none",
      "TP": 0.02,
      "SL": 0.05,
      "n": 9,
      "wr": 0.889,
      "pnl": 0.1019,
      "score": 9.058
    },
    "bear-calm": {
      "reason": "no_valid_combo"
    },
    "bear-storm": {
      "RH": 78,
      "RL": 30,
      "sigs": [
        "PV",
        "BB"
      ],
      "flt": "none",
      "TP": 0.006,
      "SL": 0.05,
      "n": 5,
      "wr": 1.0,
      "pnl": 0.0255,
      "score": 2.55
    },
    "chop": {
      "RH": 78,
      "RL": 30,
      "sigs": [
        "BB"
      ],
      "flt": "none",
      "TP": 0.02,
      "SL": 0.05,
      "n": 11,
      "wr": 0.909,
      "pnl": 0.1401,
      "score": 12.736
    }
  },
  "GMX": {
    "bull-calm": {
      "RH": 70,
      "RL": 30,
      "sigs": [
        "PV"
      ],
      "flt": "none",
      "TP": 0.02,
      "SL": 0.05,
      "n": 30,
      "wr": 0.867,
      "pnl": 0.368,
      "score": 31.889
    },
    "bull-storm": {
      "RH": 70,
      "RL": 22,
      "sigs": [
        "PV",
        "BB"
      ],
      "flt": "none",
      "TP": 0.02,
      "SL": 0.05,
      "n": 13,
      "wr": 0.846,
      "pnl": 0.1834,
      "score": 15.52
    },
    "bear-calm": {
      "reason": "no_valid_combo"
    },
    "bear-storm": {
      "RH": 78,
      "RL": 30,
      "sigs": [
        "PV",
        "BB"
      ],
      "flt": "none",
      "TP": 0.02,
      "SL": 0.05,
      "n": 6,
      "wr": 1.0,
      "pnl": 0.1146,
      "score": 11.46
    },
    "chop": {
      "RH": 78,
      "RL": 30,
      "sigs": [
        "PV"
      ],
      "flt": "none",
      "TP": 0.02,
      "SL": 0.05,
      "n": 17,
      "wr": 0.882,
      "pnl": 0.1847,
      "score": 16.297
    }
  },
  "HBAR": {
    "bull-calm": {
      "RH": 78,
      "RL": 30,
      "sigs": [
        "PV"
      ],
      "flt": "none",
      "TP": 0.02,
      "SL": 0.05,
      "n": 18,
      "wr": 0.722,
      "pnl": 0.1785,
      "score": 12.893
    },
    "bull-storm": {
      "RH": 70,
      "RL": 22,
      "sigs": [
        "PV",
        "BB"
      ],
      "flt": "none",
      "TP": 0.012,
      "SL": 0.05,
      "n": 13,
      "wr": 0.923,
      "pnl": 0.1213,
      "score": 11.197
    },
    "bear-calm": {
      "reason": "no_valid_combo"
    },
    "bear-storm": {
      "RH": 70,
      "RL": 30,
      "sigs": [
        "PV",
        "BB"
      ],
      "flt": "none",
      "TP": 0.02,
      "SL": 0.05,
      "n": 12,
      "wr": 0.917,
      "pnl": 0.1627,
      "score": 14.911
    },
    "chop": {
      "RH": 78,
      "RL": 30,
      "sigs": [
        "BB"
      ],
      "flt": "none",
      "TP": 0.02,
      "SL": 0.05,
      "n": 11,
      "wr": 0.909,
      "pnl": 0.1401,
      "score": 12.736
    }
  },
  "HYPE": {
    "bull-calm": {
      "RH": 70,
      "RL": 30,
      "sigs": [
        "PV",
        "BB"
      ],
      "flt": "none",
      "TP": 0.02,
      "SL": 0.05,
      "n": 21,
      "wr": 0.905,
      "pnl": 0.3108,
      "score": 28.121
    },
    "bull-storm": {
      "RH": 70,
      "RL": 30,
      "sigs": [
        "PV",
        "BB"
      ],
      "flt": "none",
      "TP": 0.02,
      "SL": 0.05,
      "n": 19,
      "wr": 1.0,
      "pnl": 0.3629,
      "score": 36.29
    },
    "bear-calm": {
      "reason": "no_valid_combo"
    },
    "bear-storm": {
      "RH": 78,
      "RL": 30,
      "sigs": [
        "PV",
        "BB"
      ],
      "flt": "none",
      "TP": 0.012,
      "SL": 0.05,
      "n": 6,
      "wr": 0.833,
      "pnl": 0.0456,
      "score": 3.798
    },
    "chop": {
      "RH": 78,
      "RL": 30,
      "sigs": [
        "PV",
        "BB"
      ],
      "flt": "none",
      "TP": 0.02,
      "SL": 0.05,
      "n": 19,
      "wr": 0.842,
      "pnl": 0.1704,
      "score": 14.347
    }
  },
  "IO": {
    "bull-calm": {
      "RH": 78,
      "RL": 30,
      "sigs": [
        "PV"
      ],
      "flt": "none",
      "TP": 0.02,
      "SL": 0.05,
      "n": 16,
      "wr": 0.812,
      "pnl": 0.1394,
      "score": 11.33
    },
    "bull-storm": {
      "RH": 70,
      "RL": 30,
      "sigs": [
        "PV",
        "BB"
      ],
      "flt": "none",
      "TP": 0.02,
      "SL": 0.05,
      "n": 19,
      "wr": 0.895,
      "pnl": 0.2872,
      "score": 25.692
    },
    "bear-calm": {
      "reason": "no_valid_combo"
    },
    "bear-storm": {
      "RH": 70,
      "RL": 30,
      "sigs": [
        "PV",
        "BB"
      ],
      "flt": "none",
      "TP": 0.012,
      "SL": 0.05,
      "n": 6,
      "wr": 1.0,
      "pnl": 0.0666,
      "score": 6.66
    },
    "chop": {
      "RH": 78,
      "RL": 30,
      "sigs": [
        "PV"
      ],
      "flt": "none",
      "TP": 0.02,
      "SL": 0.05,
      "n": 18,
      "wr": 0.889,
      "pnl": 0.2038,
      "score": 18.116
    }
  },
  "IOTA": {
    "bull-calm": {
      "RH": 70,
      "RL": 22,
      "sigs": [
        "PV"
      ],
      "flt": "none",
      "TP": 0.006,
      "SL": 0.05,
      "n": 14,
      "wr": 0.929,
      "pnl": 0.0398,
      "score": 3.691
    },
    "bull-storm": {
      "RH": 78,
      "RL": 30,
      "sigs": [
        "BB"
      ],
      "flt": "none",
      "TP": 0.02,
      "SL": 0.05,
      "n": 5,
      "wr": 0.0,
      "pnl": -0.1661,
      "score": -0.0
    },
    "bear-calm": {
      "reason": "no_valid_combo"
    },
    "bear-storm": {
      "RH": 70,
      "RL": 30,
      "sigs": [
        "PV",
        "BB"
      ],
      "flt": "none",
      "TP": 0.02,
      "SL": 0.05,
      "n": 10,
      "wr": 1.0,
      "pnl": 0.191,
      "score": 19.1
    },
    "chop": {
      "RH": 70,
      "RL": 30,
      "sigs": [
        "PV",
        "BB"
      ],
      "flt": "ema200",
      "TP": 0.02,
      "SL": 0.05,
      "n": 7,
      "wr": 1.0,
      "pnl": 0.1222,
      "score": 12.218
    }
  },
  "KAITO": {
    "bull-calm": {
      "RH": 78,
      "RL": 30,
      "sigs": [
        "BB"
      ],
      "flt": "none",
      "TP": 0.012,
      "SL": 0.05,
      "n": 9,
      "wr": 0.667,
      "pnl": -0.0414,
      "score": -2.763
    },
    "bull-storm": {
      "RH": 78,
      "RL": 30,
      "sigs": [
        "PV",
        "BB"
      ],
      "flt": "none",
      "TP": 0.02,
      "SL": 0.05,
      "n": 6,
      "wr": 1.0,
      "pnl": 0.1146,
      "score": 11.46
    },
    "bear-calm": {
      "reason": "no_valid_combo"
    },
    "bear-storm": {
      "reason": "no_valid_combo"
    },
    "chop": {
      "RH": 70,
      "RL": 30,
      "sigs": [
        "PV"
      ],
      "flt": "ema200",
      "TP": 0.02,
      "SL": 0.05,
      "n": 5,
      "wr": 0.6,
      "pnl": -0.0039,
      "score": -0.232
    }
  },
  "LINEA": {
    "bull-calm": {
      "RH": 70,
      "RL": 30,
      "sigs": [
        "BB"
      ],
      "flt": "none",
      "TP": 0.02,
      "SL": 0.05,
      "n": 17,
      "wr": 0.882,
      "pnl": 0.1925,
      "score": 16.982
    },
    "bull-storm": {
      "RH": 70,
      "RL": 22,
      "sigs": [
        "PV",
        "BB"
      ],
      "flt": "none",
      "TP": 0.02,
      "SL": 0.05,
      "n": 9,
      "wr": 0.889,
      "pnl": 0.1019,
      "score": 9.058
    },
    "bear-calm": {
      "reason": "no_valid_combo"
    },
    "bear-storm": {
      "RH": 70,
      "RL": 22,
      "sigs": [
        "PV"
      ],
      "flt": "none",
      "TP": 0.006,
      "SL": 0.05,
      "n": 5,
      "wr": 0.8,
      "pnl": -0.0305,
      "score": -2.44
    },
    "chop": {
      "RH": 78,
      "RL": 30,
      "sigs": [
        "BB"
      ],
      "flt": "none",
      "TP": 0.02,
      "SL": 0.05,
      "n": 10,
      "wr": 1.0,
      "pnl": 0.191,
      "score": 19.1
    }
  },
  "ME": {
    "bull-calm": {
      "RH": 70,
      "RL": 30,
      "sigs": [
        "PV"
      ],
      "flt": "none",
      "TP": 0.02,
      "SL": 0.05,
      "n": 20,
      "wr": 0.85,
      "pnl": 0.172,
      "score": 14.62
    },
    "bull-storm": {
      "RH": 70,
      "RL": 30,
      "sigs": [
        "BB"
      ],
      "flt": "none",
      "TP": 0.012,
      "SL": 0.05,
      "n": 10,
      "wr": 1.0,
      "pnl": 0.111,
      "score": 11.1
    },
    "bear-calm": {
      "reason": "no_valid_combo"
    },
    "bear-storm": {
      "RH": 70,
      "RL": 30,
      "sigs": [
        "PV",
        "BB"
      ],
      "flt": "none",
      "TP": 0.012,
      "SL": 0.05,
      "n": 10,
      "wr": 1.0,
      "pnl": 0.111,
      "score": 11.1
    },
    "chop": {
      "RH": 78,
      "RL": 30,
      "sigs": [
        "PV",
        "BB"
      ],
      "flt": "none",
      "TP": 0.012,
      "SL": 0.05,
      "n": 16,
      "wr": 0.875,
      "pnl": 0.0832,
      "score": 7.281
    }
  },
  "MEGA": {
    "bull-calm": {
      "RH": 78,
      "RL": 30,
      "sigs": [
        "PV",
        "BB"
      ],
      "flt": "none",
      "TP": 0.006,
      "SL": 0.05,
      "n": 32,
      "wr": 0.938,
      "pnl": 0.0904,
      "score": 8.473
    },
    "bull-storm": {
      "RH": 70,
      "RL": 30,
      "sigs": [
        "PV"
      ],
      "flt": "none",
      "TP": 0.02,
      "SL": 0.05,
      "n": 36,
      "wr": 0.639,
      "pnl": 0.2166,
      "score": 13.838
    },
    "bear-calm": {
      "reason": "no_valid_combo"
    },
    "bear-storm": {
      "RH": 70,
      "RL": 30,
      "sigs": [
        "PV",
        "BB"
      ],
      "flt": "none",
      "TP": 0.012,
      "SL": 0.05,
      "n": 15,
      "wr": 0.667,
      "pnl": 0.0673,
      "score": 4.485
    },
    "chop": {
      "RH": 78,
      "RL": 30,
      "sigs": [
        "PV",
        "BB"
      ],
      "flt": "ema200",
      "TP": 0.02,
      "SL": 0.05,
      "n": 18,
      "wr": 0.889,
      "pnl": 0.0683,
      "score": 6.071
    }
  },
  "MELANIA": {
    "bull-calm": {
      "RH": 78,
      "RL": 22,
      "sigs": [
        "PV"
      ],
      "flt": "none",
      "TP": 0.02,
      "SL": 0.05,
      "n": 5,
      "wr": 1.0,
      "pnl": 0.0955,
      "score": 9.55
    },
    "bull-storm": {
      "RH": 70,
      "RL": 30,
      "sigs": [
        "BB"
      ],
      "flt": "none",
      "TP": 0.02,
      "SL": 0.05,
      "n": 6,
      "wr": 1.0,
      "pnl": 0.1146,
      "score": 11.46
    },
    "bear-calm": {
      "reason": "no_valid_combo"
    },
    "bear-storm": {
      "RH": 70,
      "RL": 30,
      "sigs": [
        "PV"
      ],
      "flt": "none",
      "TP": 0.02,
      "SL": 0.05,
      "n": 7,
      "wr": 1.0,
      "pnl": 0.1337,
      "score": 13.37
    },
    "chop": {
      "RH": 78,
      "RL": 30,
      "sigs": [
        "PV"
      ],
      "flt": "none",
      "TP": 0.012,
      "SL": 0.05,
      "n": 12,
      "wr": 0.917,
      "pnl": 0.0712,
      "score": 6.527
    }
  },
  "MERL": {
    "bull-calm": {
      "RH": 78,
      "RL": 30,
      "sigs": [
        "PV"
      ],
      "flt": "none",
      "TP": 0.02,
      "SL": 0.05,
      "n": 15,
      "wr": 0.8,
      "pnl": 0.0765,
      "score": 6.12
    },
    "bull-storm": {
      "RH": 70,
      "RL": 30,
      "sigs": [
        "PV",
        "BB"
      ],
      "flt": "none",
      "TP": 0.012,
      "SL": 0.05,
      "n": 15,
      "wr": 0.933,
      "pnl": 0.1045,
      "score": 9.753
    },
    "bear-calm": {
      "reason": "no_valid_combo"
    },
    "bear-storm": {
      "RH": 70,
      "RL": 30,
      "sigs": [
        "PV"
      ],
      "flt": "none",
      "TP": 0.02,
      "SL": 0.05,
      "n": 7,
      "wr": 0.857,
      "pnl": 0.0637,
      "score": 5.46
    },
    "chop": {
      "RH": 78,
      "RL": 30,
      "sigs": [
        "PV",
        "BB"
      ],
      "flt": "none",
      "TP": 0.02,
      "SL": 0.05,
      "n": 29,
      "wr": 0.793,
      "pnl": 0.1339,
      "score": 10.62
    }
  },
  "MOVE": {
    "bull-calm": {
      "RH": 78,
      "RL": 30,
      "sigs": [
        "PV",
        "BB"
      ],
      "flt": "none",
      "TP": 0.02,
      "SL": 0.05,
      "n": 15,
      "wr": 0.933,
      "pnl": 0.2165,
      "score": 20.207
    },
    "bull-storm": {
      "RH": 70,
      "RL": 30,
      "sigs": [
        "BB"
      ],
      "flt": "none",
      "TP": 0.006,
      "SL": 0.05,
      "n": 14,
      "wr": 1.0,
      "pnl": 0.0714,
      "score": 7.14
    },
    "bear-calm": {
      "reason": "no_valid_combo"
    },
    "bear-storm": {
      "RH": 78,
      "RL": 30,
      "sigs": [
        "PV",
        "BB"
      ],
      "flt": "none",
      "TP": 0.02,
      "SL": 0.05,
      "n": 6,
      "wr": 1.0,
      "pnl": 0.1146,
      "score": 11.46
    },
    "chop": {
      "RH": 78,
      "RL": 30,
      "sigs": [
        "PV",
        "BB"
      ],
      "flt": "none",
      "TP": 0.02,
      "SL": 0.05,
      "n": 25,
      "wr": 0.92,
      "pnl": 0.3375,
      "score": 31.05
    }
  },
  "NIL": {
    "bull-calm": {
      "RH": 78,
      "RL": 22,
      "sigs": [
        "PV",
        "BB"
      ],
      "flt": "none",
      "TP": 0.012,
      "SL": 0.05,
      "n": 10,
      "wr": 0.7,
      "pnl": 0.0002,
      "score": 0.016
    },
    "bull-storm": {
      "RH": 70,
      "RL": 30,
      "sigs": [
        "PV",
        "BB"
      ],
      "flt": "none",
      "TP": 0.02,
      "SL": 0.05,
      "n": 25,
      "wr": 0.84,
      "pnl": 0.2699,
      "score": 22.668
    },
    "bear-calm": {
      "reason": "no_valid_combo"
    },
    "bear-storm": {
      "RH": 70,
      "RL": 30,
      "sigs": [
        "PV"
      ],
      "flt": "none",
      "TP": 0.012,
      "SL": 0.05,
      "n": 5,
      "wr": 1.0,
      "pnl": 0.0555,
      "score": 5.55
    },
    "chop": {
      "RH": 78,
      "RL": 30,
      "sigs": [
        "PV",
        "BB"
      ],
      "flt": "none",
      "TP": 0.02,
      "SL": 0.05,
      "n": 22,
      "wr": 0.818,
      "pnl": 0.233,
      "score": 19.06
    }
  },
  "ONDO": {
    "bull-calm": {
      "RH": 70,
      "RL": 22,
      "sigs": [
        "PV",
        "BB"
      ],
      "flt": "none",
      "TP": 0.02,
      "SL": 0.05,
      "n": 16,
      "wr": 0.875,
      "pnl": 0.2014,
      "score": 17.622
    },
    "bull-storm": {
      "RH": 70,
      "RL": 22,
      "sigs": [
        "PV"
      ],
      "flt": "none",
      "TP": 0.02,
      "SL": 0.05,
      "n": 6,
      "wr": 0.833,
      "pnl": 0.0843,
      "score": 7.023
    },
    "bear-calm": {
      "reason": "no_valid_combo"
    },
    "bear-storm": {
      "RH": 70,
      "RL": 30,
      "sigs": [
        "PV",
        "BB"
      ],
      "flt": "none",
      "TP": 0.012,
      "SL": 0.05,
      "n": 7,
      "wr": 0.714,
      "pnl": 0.0004,
      "score": 0.029
    },
    "chop": {
      "RH": 78,
      "RL": 30,
      "sigs": [
        "BB"
      ],
      "flt": "none",
      "TP": 0.02,
      "SL": 0.05,
      "n": 16,
      "wr": 1.0,
      "pnl": 0.3056,
      "score": 30.56
    }
  },
  "PAXG": {
    "bull-calm": {
      "RH": 70,
      "RL": 30,
      "sigs": [
        "PV",
        "BB"
      ],
      "flt": "none",
      "TP": 0.012,
      "SL": 0.05,
      "n": 28,
      "wr": 0.571,
      "pnl": 0.0458,
      "score": 2.616
    },
    "bull-storm": {
      "RH": 70,
      "RL": 22,
      "sigs": [
        "PV",
        "BB"
      ],
      "flt": "none",
      "TP": 0.02,
      "SL": 0.05,
      "n": 10,
      "wr": 0.7,
      "pnl": 0.0865,
      "score": 6.054
    },
    "bear-calm": {
      "reason": "no_valid_combo"
    },
    "bear-storm": {
      "RH": 78,
      "RL": 30,
      "sigs": [
        "PV",
        "BB"
      ],
      "flt": "none",
      "TP": 0.02,
      "SL": 0.05,
      "n": 8,
      "wr": 0.875,
      "pnl": 0.0859,
      "score": 7.519
    },
    "chop": {
      "RH": 78,
      "RL": 30,
      "sigs": [
        "PV"
      ],
      "flt": "none",
      "TP": 0.012,
      "SL": 0.05,
      "n": 23,
      "wr": 0.739,
      "pnl": 0.1586,
      "score": 11.725
    }
  },
  "PNUT": {
    "bull-calm": {
      "RH": 78,
      "RL": 22,
      "sigs": [
        "BB"
      ],
      "flt": "none",
      "TP": 0.006,
      "SL": 0.05,
      "n": 5,
      "wr": 1.0,
      "pnl": 0.0255,
      "score": 2.55
    },
    "bull-storm": {
      "RH": 70,
      "RL": 30,
      "sigs": [
        "PV",
        "BB"
      ],
      "flt": "none",
      "TP": 0.02,
      "SL": 0.05,
      "n": 21,
      "wr": 0.857,
      "pnl": 0.2395,
      "score": 20.528
    },
    "bear-calm": {
      "reason": "no_valid_combo"
    },
    "bear-storm": {
      "RH": 70,
      "RL": 22,
      "sigs": [
        "PV",
        "BB"
      ],
      "flt": "none",
      "TP": 0.02,
      "SL": 0.05,
      "n": 5,
      "wr": 0.0,
      "pnl": -0.2545,
      "score": -0.0
    },
    "chop": {
      "RH": 70,
      "RL": 30,
      "sigs": [
        "PV",
        "BB"
      ],
      "flt": "ema200",
      "TP": 0.02,
      "SL": 0.05,
      "n": 6,
      "wr": 0.833,
      "pnl": 0.0446,
      "score": 3.717
    }
  },
  "S": {
    "bull-calm": {
      "RH": 70,
      "RL": 22,
      "sigs": [
        "BB"
      ],
      "flt": "none",
      "TP": 0.02,
      "SL": 0.05,
      "n": 12,
      "wr": 0.833,
      "pnl": 0.123,
      "score": 10.247
    },
    "bull-storm": {
      "RH": 70,
      "RL": 22,
      "sigs": [
        "PV",
        "BB"
      ],
      "flt": "none",
      "TP": 0.02,
      "SL": 0.05,
      "n": 6,
      "wr": 0.833,
      "pnl": 0.0881,
      "score": 7.338
    },
    "bear-calm": {
      "reason": "no_valid_combo"
    },
    "bear-storm": {
      "RH": 70,
      "RL": 30,
      "sigs": [
        "PV",
        "BB"
      ],
      "flt": "none",
      "TP": 0.02,
      "SL": 0.05,
      "n": 7,
      "wr": 0.857,
      "pnl": 0.0956,
      "score": 8.192
    },
    "chop": {
      "RH": 78,
      "RL": 30,
      "sigs": [
        "PV",
        "BB"
      ],
      "flt": "none",
      "TP": 0.02,
      "SL": 0.05,
      "n": 26,
      "wr": 0.846,
      "pnl": 0.2166,
      "score": 18.328
    }
  },
  "SAGA": {
    "bull-calm": {
      "RH": 70,
      "RL": 22,
      "sigs": [
        "PV",
        "BB"
      ],
      "flt": "none",
      "TP": 0.02,
      "SL": 0.05,
      "n": 13,
      "wr": 0.692,
      "pnl": -0.0317,
      "score": -2.195
    },
    "bull-storm": {
      "RH": 70,
      "RL": 30,
      "sigs": [
        "PV",
        "BB"
      ],
      "flt": "none",
      "TP": 0.02,
      "SL": 0.05,
      "n": 24,
      "wr": 0.792,
      "pnl": 0.1746,
      "score": 13.823
    },
    "bear-calm": {
      "reason": "no_valid_combo"
    },
    "bear-storm": {
      "RH": 70,
      "RL": 30,
      "sigs": [
        "PV",
        "BB"
      ],
      "flt": "none",
      "TP": 0.02,
      "SL": 0.05,
      "n": 9,
      "wr": 1.0,
      "pnl": 0.1719,
      "score": 17.19
    },
    "chop": {
      "RH": 78,
      "RL": 30,
      "sigs": [
        "PV"
      ],
      "flt": "none",
      "TP": 0.02,
      "SL": 0.05,
      "n": 17,
      "wr": 0.824,
      "pnl": 0.1147,
      "score": 9.446
    }
  },
  "SCR": {
    "bull-calm": {
      "RH": 78,
      "RL": 30,
      "sigs": [
        "BB"
      ],
      "flt": "none",
      "TP": 0.02,
      "SL": 0.05,
      "n": 5,
      "wr": 0.8,
      "pnl": 0.0255,
      "score": 2.04
    },
    "bull-storm": {
      "RH": 70,
      "RL": 30,
      "sigs": [
        "BB"
      ],
      "flt": "none",
      "TP": 0.012,
      "SL": 0.05,
      "n": 6,
      "wr": 1.0,
      "pnl": 0.0666,
      "score": 6.66
    },
    "bear-calm": {
      "reason": "no_valid_combo"
    },
    "bear-storm": {
      "RH": 70,
      "RL": 30,
      "sigs": [
        "PV"
      ],
      "flt": "none",
      "TP": 0.02,
      "SL": 0.05,
      "n": 8,
      "wr": 0.875,
      "pnl": 0.0828,
      "score": 7.245
    },
    "chop": {
      "RH": 78,
      "RL": 30,
      "sigs": [
        "BB"
      ],
      "flt": "none",
      "TP": 0.02,
      "SL": 0.05,
      "n": 5,
      "wr": 0.8,
      "pnl": 0.0713,
      "score": 5.705
    }
  },
  "SEI": {
    "bull-calm": {
      "RH": 70,
      "RL": 22,
      "sigs": [
        "PV",
        "BB"
      ],
      "flt": "none",
      "TP": 0.02,
      "SL": 0.05,
      "n": 10,
      "wr": 0.7,
      "pnl": 0.0338,
      "score": 2.363
    },
    "bull-storm": {
      "RH": 70,
      "RL": 22,
      "sigs": [
        "PV"
      ],
      "flt": "none",
      "TP": 0.02,
      "SL": 0.05,
      "n": 8,
      "wr": 1.0,
      "pnl": 0.1366,
      "score": 13.658
    },
    "bear-calm": {
      "reason": "no_valid_combo"
    },
    "bear-storm": {
      "RH": 70,
      "RL": 30,
      "sigs": [
        "PV",
        "BB"
      ],
      "flt": "none",
      "TP": 0.012,
      "SL": 0.05,
      "n": 6,
      "wr": 1.0,
      "pnl": 0.0666,
      "score": 6.66
    },
    "chop": {
      "RH": 78,
      "RL": 30,
      "sigs": [
        "PV"
      ],
      "flt": "none",
      "TP": 0.02,
      "SL": 0.05,
      "n": 16,
      "wr": 0.875,
      "pnl": 0.1656,
      "score": 14.49
    }
  },
  "TNSR": {
    "bull-calm": {
      "RH": 70,
      "RL": 22,
      "sigs": [
        "PV"
      ],
      "flt": "none",
      "TP": 0.02,
      "SL": 0.05,
      "n": 24,
      "wr": 0.75,
      "pnl": 0.0577,
      "score": 4.324
    },
    "bull-storm": {
      "RH": 78,
      "RL": 30,
      "sigs": [
        "PV"
      ],
      "flt": "none",
      "TP": 0.02,
      "SL": 0.05,
      "n": 22,
      "wr": 0.818,
      "pnl": 0.1402,
      "score": 11.471
    },
    "bear-calm": {
      "reason": "insufficient_bars_48",
      "n": 0
    },
    "bear-storm": {
      "RH": 78,
      "RL": 30,
      "sigs": [
        "PV",
        "BB"
      ],
      "flt": "none",
      "TP": 0.02,
      "SL": 0.05,
      "n": 5,
      "wr": 0.8,
      "pnl": 0.055,
      "score": 4.399
    },
    "chop": {
      "RH": 78,
      "RL": 30,
      "sigs": [
        "PV",
        "BB"
      ],
      "flt": "ema200",
      "TP": 0.02,
      "SL": 0.05,
      "n": 12,
      "wr": 1.0,
      "pnl": 0.2123,
      "score": 21.228
    }
  },
  "TST": {
    "bull-calm": {
      "RH": 78,
      "RL": 22,
      "sigs": [
        "PV",
        "BB"
      ],
      "flt": "none",
      "TP": 0.012,
      "SL": 0.05,
      "n": 6,
      "wr": 1.0,
      "pnl": 0.0666,
      "score": 6.66
    },
    "bull-storm": {
      "RH": 70,
      "RL": 30,
      "sigs": [
        "BB"
      ],
      "flt": "none",
      "TP": 0.02,
      "SL": 0.05,
      "n": 5,
      "wr": 1.0,
      "pnl": 0.0955,
      "score": 9.55
    },
    "bear-calm": {
      "reason": "no_valid_combo"
    },
    "bear-storm": {
      "RH": 70,
      "RL": 30,
      "sigs": [
        "PV"
      ],
      "flt": "none",
      "TP": 0.012,
      "SL": 0.05,
      "n": 9,
      "wr": 0.778,
      "pnl": -0.0241,
      "score": -1.874
    },
    "chop": {
      "RH": 78,
      "RL": 30,
      "sigs": [
        "BB"
      ],
      "flt": "none",
      "TP": 0.02,
      "SL": 0.05,
      "n": 11,
      "wr": 0.909,
      "pnl": 0.1401,
      "score": 12.736
    }
  },
  "USUAL": {
    "bull-calm": {
      "RH": 78,
      "RL": 30,
      "sigs": [
        "PV",
        "BB"
      ],
      "flt": "none",
      "TP": 0.02,
      "SL": 0.05,
      "n": 23,
      "wr": 0.87,
      "pnl": 0.2891,
      "score": 25.14
    },
    "bull-storm": {
      "RH": 70,
      "RL": 22,
      "sigs": [
        "PV",
        "BB"
      ],
      "flt": "none",
      "TP": 0.02,
      "SL": 0.05,
      "n": 18,
      "wr": 0.722,
      "pnl": 0.0938,
      "score": 6.774
    },
    "bear-calm": {
      "reason": "no_valid_combo"
    },
    "bear-storm": {
      "RH": 70,
      "RL": 30,
      "sigs": [
        "PV"
      ],
      "flt": "none",
      "TP": 0.012,
      "SL": 0.05,
      "n": 16,
      "wr": 0.875,
      "pnl": 0.1228,
      "score": 10.742
    },
    "chop": {
      "RH": 78,
      "RL": 30,
      "sigs": [
        "PV",
        "BB"
      ],
      "flt": "none",
      "TP": 0.02,
      "SL": 0.05,
      "n": 33,
      "wr": 0.788,
      "pnl": 0.1403,
      "score": 11.054
    }
  },
  "XMR": {
    "bull-calm": {
      "RH": 78,
      "RL": 30,
      "sigs": [
        "PV",
        "BB"
      ],
      "flt": "none",
      "TP": 0.02,
      "SL": 0.05,
      "n": 15,
      "wr": 0.733,
      "pnl": 0.142,
      "score": 10.41
    },
    "bull-storm": {
      "RH": 78,
      "RL": 30,
      "sigs": [
        "PV",
        "BB"
      ],
      "flt": "none",
      "TP": 0.02,
      "SL": 0.05,
      "n": 10,
      "wr": 0.8,
      "pnl": 0.1197,
      "score": 9.58
    },
    "bear-calm": {
      "reason": "no_valid_combo"
    },
    "bear-storm": {
      "RH": 70,
      "RL": 30,
      "sigs": [
        "BB"
      ],
      "flt": "none",
      "TP": 0.02,
      "SL": 0.05,
      "n": 5,
      "wr": 1.0,
      "pnl": 0.0673,
      "score": 6.732
    },
    "chop": {
      "RH": 78,
      "RL": 30,
      "sigs": [
        "BB"
      ],
      "flt": "none",
      "TP": 0.012,
      "SL": 0.05,
      "n": 10,
      "wr": 0.9,
      "pnl": 0.049,
      "score": 4.41
    }
  },
  "ZEREBRO": {
    "bull-calm": {
      "RH": 78,
      "RL": 22,
      "sigs": [
        "PV"
      ],
      "flt": "none",
      "TP": 0.02,
      "SL": 0.05,
      "n": 10,
      "wr": 0.9,
      "pnl": 0.121,
      "score": 10.89
    },
    "bull-storm": {
      "RH": 78,
      "RL": 30,
      "sigs": [
        "PV",
        "BB"
      ],
      "flt": "none",
      "TP": 0.006,
      "SL": 0.05,
      "n": 7,
      "wr": 1.0,
      "pnl": 0.0357,
      "score": 3.57
    },
    "bear-calm": {
      "reason": "no_valid_combo"
    },
    "bear-storm": {
      "RH": 70,
      "RL": 22,
      "sigs": [
        "PV",
        "BB"
      ],
      "flt": "none",
      "TP": 0.006,
      "SL": 0.05,
      "n": 8,
      "wr": 1.0,
      "pnl": 0.0408,
      "score": 4.08
    },
    "chop": {
      "RH": 78,
      "RL": 30,
      "sigs": [
        "PV",
        "BB"
      ],
      "flt": "none",
      "TP": 0.02,
      "SL": 0.05,
      "n": 20,
      "wr": 0.8,
      "pnl": 0.102,
      "score": 8.16
    }
  },
  "ZORA": {
    "bull-calm": {
      "RH": 70,
      "RL": 22,
      "sigs": [
        "PV",
        "BB"
      ],
      "flt": "none",
      "TP": 0.006,
      "SL": 0.05,
      "n": 17,
      "wr": 1.0,
      "pnl": 0.0867,
      "score": 8.67
    },
    "bull-storm": {
      "RH": 70,
      "RL": 22,
      "sigs": [
        "PV",
        "BB"
      ],
      "flt": "none",
      "TP": 0.02,
      "SL": 0.05,
      "n": 8,
      "wr": 0.875,
      "pnl": 0.1172,
      "score": 10.252
    },
    "bear-calm": {
      "reason": "no_valid_combo"
    },
    "bear-storm": {
      "RH": 70,
      "RL": 30,
      "sigs": [
        "PV",
        "BB"
      ],
      "flt": "none",
      "TP": 0.02,
      "SL": 0.05,
      "n": 5,
      "wr": 0.8,
      "pnl": 0.0746,
      "score": 5.97
    },
    "chop": {
      "RH": 78,
      "RL": 22,
      "sigs": [
        "BB"
      ],
      "flt": "none",
      "TP": 0.006,
      "SL": 0.05,
      "n": 5,
      "wr": 1.0,
      "pnl": 0.0255,
      "score": 2.55
    }
  },
  "APT": {
    "bull-calm": {
      "RH": 78,
      "RL": 22,
      "sigs": [
        "PV"
      ],
      "flt": "none",
      "TP": 0.006,
      "SL": 0.05,
      "n": 5,
      "wr": 1.0,
      "pnl": 0.0255,
      "score": 2.55
    },
    "bull-storm": {
      "RH": 70,
      "RL": 22,
      "sigs": [
        "PV",
        "BB"
      ],
      "flt": "none",
      "TP": 0.02,
      "SL": 0.05,
      "n": 10,
      "wr": 0.9,
      "pnl": 0.121,
      "score": 10.89
    },
    "bear-calm": {
      "RH": 78,
      "RL": 30,
      "sigs": [
        "PV",
        "BB"
      ],
      "flt": "none",
      "TP": 0.02,
      "SL": 0.05,
      "n": 9,
      "wr": 0.889,
      "pnl": 0.1336,
      "score": 11.879
    },
    "bear-storm": {
      "RH": 70,
      "RL": 30,
      "sigs": [
        "BB"
      ],
      "flt": "none",
      "TP": 0.02,
      "SL": 0.05,
      "n": 5,
      "wr": 0.8,
      "pnl": 0.0255,
      "score": 2.04
    },
    "chop": {
      "RH": 78,
      "RL": 30,
      "sigs": [
        "BB"
      ],
      "flt": "none",
      "TP": 0.02,
      "SL": 0.05,
      "n": 17,
      "wr": 1.0,
      "pnl": 0.3247,
      "score": 32.47
    }
  },
  "AVAX": {
    "bull-calm": {
      "RH": 70,
      "RL": 22,
      "sigs": [
        "BB"
      ],
      "flt": "none",
      "TP": 0.02,
      "SL": 0.05,
      "n": 7,
      "wr": 0.714,
      "pnl": -0.0063,
      "score": -0.45
    },
    "bull-storm": {
      "RH": 70,
      "RL": 30,
      "sigs": [
        "BB"
      ],
      "flt": "none",
      "TP": 0.02,
      "SL": 0.05,
      "n": 16,
      "wr": 0.938,
      "pnl": 0.2731,
      "score": 25.6
    },
    "bear-calm": {
      "RH": 78,
      "RL": 30,
      "sigs": [
        "PV"
      ],
      "flt": "none",
      "TP": 0.012,
      "SL": 0.05,
      "n": 7,
      "wr": 1.0,
      "pnl": 0.0689,
      "score": 6.887
    },
    "bear-storm": {
      "RH": 70,
      "RL": 30,
      "sigs": [
        "BB"
      ],
      "flt": "none",
      "TP": 0.006,
      "SL": 0.05,
      "n": 5,
      "wr": 1.0,
      "pnl": 0.0255,
      "score": 2.55
    },
    "chop": {
      "RH": 78,
      "RL": 30,
      "sigs": [
        "PV",
        "BB"
      ],
      "flt": "none",
      "TP": 0.02,
      "SL": 0.05,
      "n": 28,
      "wr": 0.893,
      "pnl": 0.3141,
      "score": 28.043
    }
  },
  "BRETT": {
    "bull-calm": {
      "RH": 78,
      "RL": 22,
      "sigs": [
        "PV",
        "BB"
      ],
      "flt": "none",
      "TP": 0.02,
      "SL": 0.05,
      "n": 7,
      "wr": 1.0,
      "pnl": 0.1337,
      "score": 13.37
    },
    "bull-storm": {
      "RH": 70,
      "RL": 30,
      "sigs": [
        "PV",
        "BB"
      ],
      "flt": "none",
      "TP": 0.02,
      "SL": 0.05,
      "n": 17,
      "wr": 0.941,
      "pnl": 0.2987,
      "score": 28.112
    },
    "bear-calm": {
      "reason": "no_valid_combo"
    },
    "bear-storm": {
      "RH": 70,
      "RL": 30,
      "sigs": [
        "PV",
        "BB"
      ],
      "flt": "none",
      "TP": 0.006,
      "SL": 0.05,
      "n": 11,
      "wr": 1.0,
      "pnl": 0.0561,
      "score": 5.61
    },
    "chop": {
      "RH": 78,
      "RL": 30,
      "sigs": [
        "PV",
        "BB"
      ],
      "flt": "none",
      "TP": 0.02,
      "SL": 0.05,
      "n": 16,
      "wr": 0.938,
      "pnl": 0.2356,
      "score": 22.087
    }
  },
  "ETC": {
    "bull-calm": {
      "RH": 70,
      "RL": 22,
      "sigs": [
        "PV"
      ],
      "flt": "none",
      "TP": 0.006,
      "SL": 0.05,
      "n": 11,
      "wr": 1.0,
      "pnl": 0.0561,
      "score": 5.61
    },
    "bull-storm": {
      "RH": 70,
      "RL": 30,
      "sigs": [
        "PV",
        "BB"
      ],
      "flt": "none",
      "TP": 0.012,
      "SL": 0.05,
      "n": 16,
      "wr": 0.812,
      "pnl": 0.0855,
      "score": 6.944
    },
    "bear-calm": {
      "reason": "no_valid_combo"
    },
    "bear-storm": {
      "RH": 70,
      "RL": 30,
      "sigs": [
        "PV",
        "BB"
      ],
      "flt": "none",
      "TP": 0.006,
      "SL": 0.05,
      "n": 8,
      "wr": 0.875,
      "pnl": 0.0219,
      "score": 1.914
    },
    "chop": {
      "RH": 70,
      "RL": 30,
      "sigs": [
        "PV"
      ],
      "flt": "none",
      "TP": 0.012,
      "SL": 0.05,
      "n": 29,
      "wr": 0.897,
      "pnl": 0.1359,
      "score": 12.184
    }
  },
  "GALA": {
    "bull-calm": {
      "RH": 70,
      "RL": 22,
      "sigs": [
        "PV"
      ],
      "flt": "none",
      "TP": 0.02,
      "SL": 0.05,
      "n": 9,
      "wr": 1.0,
      "pnl": 0.1719,
      "score": 17.19
    },
    "bull-storm": {
      "RH": 70,
      "RL": 30,
      "sigs": [
        "PV"
      ],
      "flt": "none",
      "TP": 0.02,
      "SL": 0.05,
      "n": 12,
      "wr": 0.833,
      "pnl": 0.134,
      "score": 11.166
    },
    "bear-calm": {
      "RH": 70,
      "RL": 30,
      "sigs": [
        "PV",
        "BB"
      ],
      "flt": "none",
      "TP": 0.02,
      "SL": 0.05,
      "n": 6,
      "wr": 1.0,
      "pnl": 0.1146,
      "score": 11.46
    },
    "bear-storm": {
      "reason": "no_valid_combo"
    },
    "chop": {
      "RH": 78,
      "RL": 30,
      "sigs": [
        "PV",
        "BB"
      ],
      "flt": "none",
      "TP": 0.02,
      "SL": 0.05,
      "n": 21,
      "wr": 0.857,
      "pnl": 0.1911,
      "score": 16.38
    }
  },
  "LINK": {
    "bull-calm": {
      "RH": 78,
      "RL": 30,
      "sigs": [
        "PV",
        "BB"
      ],
      "flt": "none",
      "TP": 0.02,
      "SL": 0.05,
      "n": 15,
      "wr": 0.733,
      "pnl": 0.1138,
      "score": 8.349
    },
    "bull-storm": {
      "RH": 70,
      "RL": 30,
      "sigs": [
        "PV"
      ],
      "flt": "none",
      "TP": 0.02,
      "SL": 0.05,
      "n": 17,
      "wr": 0.824,
      "pnl": 0.2213,
      "score": 18.221
    },
    "bear-calm": {
      "RH": 70,
      "RL": 30,
      "sigs": [
        "PV"
      ],
      "flt": "none",
      "TP": 0.02,
      "SL": 0.05,
      "n": 13,
      "wr": 0.923,
      "pnl": 0.1999,
      "score": 18.454
    },
    "bear-storm": {
      "RH": 70,
      "RL": 22,
      "sigs": [
        "PV",
        "BB"
      ],
      "flt": "none",
      "TP": 0.02,
      "SL": 0.05,
      "n": 5,
      "wr": 0.0,
      "pnl": -0.2545,
      "score": -0.0
    },
    "chop": {
      "RH": 78,
      "RL": 30,
      "sigs": [
        "PV"
      ],
      "flt": "none",
      "TP": 0.02,
      "SL": 0.05,
      "n": 20,
      "wr": 1.0,
      "pnl": 0.382,
      "score": 38.2
    }
  },
  "MEME": {
    "bull-calm": {
      "RH": 78,
      "RL": 30,
      "sigs": [
        "PV"
      ],
      "flt": "none",
      "TP": 0.02,
      "SL": 0.05,
      "n": 30,
      "wr": 0.8,
      "pnl": 0.2375,
      "score": 19.0
    },
    "bull-storm": {
      "RH": 70,
      "RL": 30,
      "sigs": [
        "PV"
      ],
      "flt": "none",
      "TP": 0.02,
      "SL": 0.05,
      "n": 27,
      "wr": 0.778,
      "pnl": 0.1856,
      "score": 14.437
    },
    "bear-calm": {
      "reason": "insufficient_bars_44",
      "n": 0
    },
    "bear-storm": {
      "RH": 78,
      "RL": 30,
      "sigs": [
        "PV"
      ],
      "flt": "none",
      "TP": 0.02,
      "SL": 0.05,
      "n": 10,
      "wr": 0.9,
      "pnl": 0.121,
      "score": 10.89
    },
    "chop": {
      "RH": 78,
      "RL": 30,
      "sigs": [
        "PV"
      ],
      "flt": "ema200",
      "TP": 0.02,
      "SL": 0.05,
      "n": 6,
      "wr": 0.833,
      "pnl": 0.0446,
      "score": 3.717
    }
  },
  "SUI": {
    "bull-calm": {
      "RH": 70,
      "RL": 22,
      "sigs": [
        "PV"
      ],
      "flt": "none",
      "TP": 0.012,
      "SL": 0.05,
      "n": 11,
      "wr": 0.909,
      "pnl": 0.0789,
      "score": 7.174
    },
    "bull-storm": {
      "RH": 70,
      "RL": 30,
      "sigs": [
        "PV",
        "BB"
      ],
      "flt": "none",
      "TP": 0.02,
      "SL": 0.05,
      "n": 17,
      "wr": 0.882,
      "pnl": 0.2554,
      "score": 22.539
    },
    "bear-calm": {
      "RH": 70,
      "RL": 30,
      "sigs": [
        "PV",
        "BB"
      ],
      "flt": "none",
      "TP": 0.02,
      "SL": 0.05,
      "n": 10,
      "wr": 0.9,
      "pnl": 0.1622,
      "score": 14.595
    },
    "bear-storm": {
      "RH": 70,
      "RL": 30,
      "sigs": [
        "PV",
        "BB"
      ],
      "flt": "none",
      "TP": 0.006,
      "SL": 0.05,
      "n": 7,
      "wr": 1.0,
      "pnl": 0.0357,
      "score": 3.57
    },
    "chop": {
      "RH": 70,
      "RL": 30,
      "sigs": [
        "PV",
        "BB"
      ],
      "flt": "ema200",
      "TP": 0.006,
      "SL": 0.05,
      "n": 10,
      "wr": 1.0,
      "pnl": 0.051,
      "score": 5.1
    }
  },
  "TRX": {
    "bull-calm": {
      "RH": 78,
      "RL": 30,
      "sigs": [
        "BB"
      ],
      "flt": "none",
      "TP": 0.012,
      "SL": 0.05,
      "n": 11,
      "wr": 0.727,
      "pnl": 0.0465,
      "score": 3.383
    },
    "bull-storm": {
      "RH": 78,
      "RL": 30,
      "sigs": [
        "BB"
      ],
      "flt": "none",
      "TP": 0.012,
      "SL": 0.05,
      "n": 6,
      "wr": 0.667,
      "pnl": 0.0303,
      "score": 2.02
    },
    "bear-calm": {
      "reason": "no_valid_combo"
    },
    "bear-storm": {
      "RH": 70,
      "RL": 30,
      "sigs": [
        "PV",
        "BB"
      ],
      "flt": "none",
      "TP": 0.006,
      "SL": 0.05,
      "n": 8,
      "wr": 1.0,
      "pnl": 0.0377,
      "score": 3.772
    },
    "chop": {
      "RH": 78,
      "RL": 30,
      "sigs": [
        "PV",
        "BB"
      ],
      "flt": "none",
      "TP": 0.02,
      "SL": 0.05,
      "n": 23,
      "wr": 0.739,
      "pnl": 0.1164,
      "score": 8.607
    }
  },
  "VVV": {
    "bull-calm": {
      "RH": 70,
      "RL": 30,
      "sigs": [
        "BB"
      ],
      "flt": "none",
      "TP": 0.02,
      "SL": 0.05,
      "n": 25,
      "wr": 0.88,
      "pnl": 0.2675,
      "score": 23.54
    },
    "bull-storm": {
      "RH": 78,
      "RL": 30,
      "sigs": [
        "BB"
      ],
      "flt": "none",
      "TP": 0.02,
      "SL": 0.05,
      "n": 9,
      "wr": 0.889,
      "pnl": 0.1019,
      "score": 9.058
    },
    "bear-calm": {
      "RH": 78,
      "RL": 30,
      "sigs": [
        "PV",
        "BB"
      ],
      "flt": "none",
      "TP": 0.02,
      "SL": 0.05,
      "n": 6,
      "wr": 0.833,
      "pnl": 0.0446,
      "score": 3.717
    },
    "bear-storm": {
      "RH": 70,
      "RL": 30,
      "sigs": [
        "PV"
      ],
      "flt": "none",
      "TP": 0.006,
      "SL": 0.05,
      "n": 6,
      "wr": 1.0,
      "pnl": 0.0306,
      "score": 3.06
    },
    "chop": {
      "RH": 78,
      "RL": 30,
      "sigs": [
        "PV"
      ],
      "flt": "none",
      "TP": 0.02,
      "SL": 0.05,
      "n": 23,
      "wr": 0.87,
      "pnl": 0.2293,
      "score": 19.939
    }
  },
  "XRP": {
    "bull-calm": {
      "RH": 70,
      "RL": 22,
      "sigs": [
        "BB"
      ],
      "flt": "none",
      "TP": 0.012,
      "SL": 0.05,
      "n": 8,
      "wr": 0.875,
      "pnl": 0.0695,
      "score": 6.08
    },
    "bull-storm": {
      "RH": 70,
      "RL": 30,
      "sigs": [
        "BB"
      ],
      "flt": "none",
      "TP": 0.012,
      "SL": 0.05,
      "n": 11,
      "wr": 1.0,
      "pnl": 0.1221,
      "score": 12.21
    },
    "bear-calm": {
      "RH": 70,
      "RL": 30,
      "sigs": [
        "PV"
      ],
      "flt": "none",
      "TP": 0.02,
      "SL": 0.05,
      "n": 11,
      "wr": 0.818,
      "pnl": 0.1189,
      "score": 9.727
    },
    "bear-storm": {
      "RH": 70,
      "RL": 30,
      "sigs": [
        "BB"
      ],
      "flt": "none",
      "TP": 0.006,
      "SL": 0.05,
      "n": 5,
      "wr": 0.8,
      "pnl": 0.0083,
      "score": 0.66
    },
    "chop": {
      "RH": 78,
      "RL": 30,
      "sigs": [
        "PV",
        "BB"
      ],
      "flt": "none",
      "TP": 0.02,
      "SL": 0.05,
      "n": 28,
      "wr": 0.893,
      "pnl": 0.3407,
      "score": 30.417
    }
  }
}


def get_config_for_regime(coin, regime):
    """Returns config dict for coin+regime, or None if no valid config."""
    coin_data = REGIME_CONFIGS.get(coin)
    if not coin_data: return None
    cfg = coin_data.get(regime)
    if cfg and 'TP' in cfg:
        return cfg
    return None

def get_config_with_fallback(coin, regime, fallback_chain=None):
    """Try requested regime, then fallback chain (bear-calm -> chop, etc).
    Returns first valid config."""
    if fallback_chain is None:
        # Default fallbacks: similar regimes
        fallback_chain = {
            'bear-calm':  ['chop', 'bear-storm', 'bull-calm'],
            'bull-calm':  ['chop', 'bull-storm', 'bear-calm'],
            'bull-storm': ['bull-calm', 'chop', 'bear-storm'],
            'bear-storm': ['bear-calm', 'chop', 'bull-storm'],
            'chop':       ['bull-calm', 'bear-calm', 'bull-storm'],
        }
    cfg = get_config_for_regime(coin, regime)
    if cfg: return cfg, regime
    for fb_regime in fallback_chain.get(regime, []):
        cfg = get_config_for_regime(coin, fb_regime)
        if cfg: return cfg, fb_regime
    return None, None

def coverage_stats():
    """Return coverage % per regime."""
    from collections import defaultdict
    stats = defaultdict(int)
    for coin, regs in REGIME_CONFIGS.items():
        for r, c in regs.items():
            if c and 'TP' in c:
                stats[r] += 1
    total = len(REGIME_CONFIGS)
    return {r: f"{n}/{total} ({n/total*100:.0f}%)" for r, n in stats.items()}
