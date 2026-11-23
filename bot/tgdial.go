package main

import (
	"context"
	"log"
	"net"
	"net/http"
	"os"
	"strings"
	"time"
)

const (
	telegramAPIHost = "api.telegram.org"
	pinnedAttempts  = 4
)

func pinTelegramIP(transport *http.Transport) {
	ip := strings.TrimSpace(os.Getenv("TELEGRAM_API_IP"))
	if ip == "" {
		return
	}
	if net.ParseIP(ip) == nil {
		log.Printf("TELEGRAM_API_IP=%q — не IP-адрес, игнорирую", ip)
		return
	}

	transport.DialContext = pinnedDialer(ip, &net.Dialer{Timeout: 4 * time.Second, KeepAlive: 30 * time.Second})
	log.Printf("Telegram: подключаюсь к %s по адресу %s", telegramAPIHost, ip)
}

func pinnedDialer(ip string, d *net.Dialer) func(ctx context.Context, network, addr string) (net.Conn, error) {
	return func(ctx context.Context, network, addr string) (net.Conn, error) {
		if host, port, err := net.SplitHostPort(addr); err == nil && strings.EqualFold(host, telegramAPIHost) {
			var lastErr error
			for i := 0; i < pinnedAttempts; i++ {
				c, err := d.DialContext(ctx, "tcp", net.JoinHostPort(ip, port))
				if err == nil {
					return c, nil
				}
				if ctx.Err() != nil {
					return nil, ctx.Err()
				}
				lastErr = err
			}
			log.Printf("Telegram: %s не отвечает (%v), пробую адрес из DNS", ip, lastErr)
		}
		return d.DialContext(ctx, network, addr)
	}
}
