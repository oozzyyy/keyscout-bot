package main

import (
	"testing"
	"time"
)

func TestParseEvery(t *testing.T) {
	cases := map[string]time.Duration{
		"45 мин": 45 * time.Minute, "2 ч": 2 * time.Hour, "90": 90 * time.Minute,
		"1 ч 30 мин": 90 * time.Minute, "1.5 часа": 90 * time.Minute, "1,5 ч": 90 * time.Minute,
		"3 часа": 3 * time.Hour, "1 сутки": 24 * time.Hour, "2 дня": 48 * time.Hour,
		"30m": 30 * time.Minute, "2h": 2 * time.Hour, "15 минут": 15 * time.Minute,
		"2ч30мин": 0, "раз в час": 0, "": 0, "abc": 0,
	}
	for in, want := range cases {
		got, ok := parseEvery(in)
		if want == 0 && ok && in != "2ч30мин" {
			t.Errorf("%q: ждали ошибку, получили %v", in, got)
		}
		if want != 0 && (!ok || got != want) {
			t.Errorf("%q: ждали %v, получили %v (ok=%v)", in, want, got, ok)
		}
	}
	for d, want := range map[time.Duration]string{30 * time.Minute: "30 мин", time.Hour: "1 ч",
		90 * time.Minute: "1 ч 30 мин", 24 * time.Hour: "1 сут"} {
		if got := humanEvery(d); got != want {
			t.Errorf("humanEvery(%v) = %q, want %q", d, got, want)
		}
	}
}
