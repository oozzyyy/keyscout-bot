package main

import (
	"context"
	"net"
	"testing"
	"time"
)

func TestPinnedDialer(t *testing.T) {
	ln, err := net.Listen("tcp", "127.0.0.1:0")
	if err != nil {
		t.Fatal(err)
	}
	defer ln.Close()
	_, port, _ := net.SplitHostPort(ln.Addr().String())
	got := make(chan string, 1)
	go func() {
		c, err := ln.Accept()
		if err == nil {
			got <- c.LocalAddr().String()
			c.Close()
		}
	}()

	dial := pinnedDialer("127.0.0.1", &net.Dialer{Timeout: 2 * time.Second})
	c, err := dial(context.Background(), "tcp", "api.telegram.org:"+port)
	if err != nil {
		t.Fatalf("api.telegram.org не ушёл на закреплённый адрес: %v", err)
	}
	c.Close()
	if addr := <-got; addr != ln.Addr().String() {
		t.Errorf("подключились к %s, ждали %s", addr, ln.Addr())
	}

	c, err = dial(context.Background(), "tcp", "localhost:"+port)
	if err == nil {
		c.Close()
	}
}
