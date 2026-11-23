package main

import (
	"encoding/json"
	"os"
	"sync"
	"time"
)

const (
	StateIdle         = ""
	StateWaitKeywords = "wait_keywords"
	StateWaitSites    = "wait_sites"
	StateWaitEmail    = "wait_email"
	StateAddSite      = "add_site"
	StateWaitAuto     = "wait_auto"
)

const maxHistory = 48

type HistoryPoint struct {
	CheckedAt string         `json:"checked_at"`
	Totals    map[string]int `json:"totals"`
}

type User struct {
	ChatID   int64    `json:"chat_id"`
	Username string   `json:"username"`
	Name     string   `json:"name"`
	Keywords []string `json:"keywords"`
	Sites    []string `json:"sites"`
	Email    string   `json:"email"`
	Auto     bool     `json:"auto"`

	AutoEveryMin int `json:"auto_every_min"`

	WindowHours int       `json:"window_hours"`
	LastAuto    time.Time `json:"last_auto"`
	State       string    `json:"state"`
	Onboard     bool      `json:"onboard"`

	PendingReport bool           `json:"pending_report"`
	History       []HistoryPoint `json:"history"`
}

type Store struct {
	mu    sync.Mutex
	path  string
	Users map[int64]*User `json:"users"`
}

func LoadStore(path string) (*Store, error) {
	s := &Store{path: path, Users: map[int64]*User{}}
	data, err := os.ReadFile(path)
	if os.IsNotExist(err) {
		return s, nil
	}
	if err != nil {
		return nil, err
	}
	return s, json.Unmarshal(data, s)
}

func (s *Store) Update(chatID int64, fn func(u *User)) User {
	s.mu.Lock()
	defer s.mu.Unlock()
	u, ok := s.Users[chatID]
	if !ok {
		u = &User{ChatID: chatID}
		s.Users[chatID] = u
	}
	if fn != nil {
		fn(u)
		s.saveLocked()
	}
	cp := *u
	cp.Keywords = append([]string(nil), u.Keywords...)
	cp.Sites = append([]string(nil), u.Sites...)
	cp.History = append([]HistoryPoint(nil), u.History...)
	return cp
}

func (s *Store) Get(chatID int64) User { return s.Update(chatID, nil) }

func (s *Store) Lookup(chatID int64) (User, bool) {
	s.mu.Lock()
	_, ok := s.Users[chatID]
	s.mu.Unlock()
	if !ok {
		return User{}, false
	}
	return s.Get(chatID), true
}

func (s *Store) DueForAuto(def time.Duration) []int64 {
	s.mu.Lock()
	defer s.mu.Unlock()
	var ids []int64
	for id, u := range s.Users {
		interval := def
		if u.AutoEveryMin > 0 {
			interval = time.Duration(u.AutoEveryMin) * time.Minute
		}
		if u.Auto && len(u.Keywords) > 0 && len(u.Sites) > 0 && time.Since(u.LastAuto) >= interval {
			ids = append(ids, id)
		}
	}
	return ids
}

func (s *Store) saveLocked() {
	data, _ := json.MarshalIndent(s, "", "  ")
	tmp := s.path + ".tmp"
	if err := os.WriteFile(tmp, data, 0o600); err == nil {
		_ = os.Rename(tmp, s.path)
	}
}

func (u *User) AddHistory(p HistoryPoint) {
	u.History = append(u.History, p)
	if len(u.History) > maxHistory {
		u.History = u.History[len(u.History)-maxHistory:]
	}
}

func (s *Store) MigrateSite(from, to string) {
	s.mu.Lock()
	defer s.mu.Unlock()
	changed := false
	for _, u := range s.Users {
		for i, site := range u.Sites {
			if site == from {
				u.Sites[i], changed = to, true
			}
		}
	}
	if changed {
		s.saveLocked()
	}
}

func (u *User) Window() int {
	if u.WindowHours == 12 {
		return 12
	}
	return 24
}

func (s *Store) MigrateDefaultSites(old, new []string) {
	s.mu.Lock()
	defer s.mu.Unlock()
	same := func(a, b []string) bool {
		if len(a) != len(b) {
			return false
		}
		set := map[string]bool{}
		for _, x := range a {
			set[x] = true
		}
		for _, x := range b {
			if !set[x] {
				return false
			}
		}
		return true
	}
	changed := false
	for _, u := range s.Users {
		if same(u.Sites, old) {
			u.Sites = append([]string(nil), new...)
			changed = true
		}
	}
	if changed {
		s.saveLocked()
	}
}
