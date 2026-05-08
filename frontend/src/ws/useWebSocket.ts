import { useEffect, useRef, useState, useCallback } from 'react';
import type { WsEvent } from '../api/types';

const WS_URL = import.meta.env.VITE_WS_URL || (import.meta.env.PROD
  ? `${window.location.protocol === 'https:' ? 'wss' : 'ws'}://${window.location.host}`
  : 'ws://localhost:8000');
const MAX_BACKOFF = 30_000;
const HEARTBEAT_INTERVAL = 30_000;

type WsStatus = 'connecting' | 'connected' | 'disconnected';

export default function useWebSocket() {
  const [status, setStatus] = useState<WsStatus>('disconnected');
  const [lastEvent, setLastEvent] = useState<WsEvent | null>(null);
  const wsRef = useRef<WebSocket | null>(null);
  const backoffRef = useRef(1000);
  const heartbeatRef = useRef<ReturnType<typeof setInterval> | null>(null);
  const reconnectRef = useRef<ReturnType<typeof setTimeout> | null>(null);
  const mountedRef = useRef(true);

  const cleanup = useCallback(() => {
    if (heartbeatRef.current) clearInterval(heartbeatRef.current);
    if (reconnectRef.current) clearTimeout(reconnectRef.current);
    if (wsRef.current) {
      wsRef.current.onclose = null;
      wsRef.current.close();
      wsRef.current = null;
    }
  }, []);

  const connect = useCallback(() => {
    if (!mountedRef.current) return;
    cleanup();
    setStatus('connecting');

    const ws = new WebSocket(`${WS_URL}/api/ws`);
    wsRef.current = ws;

    ws.onopen = () => {
      if (!mountedRef.current) return;
      setStatus('connected');
      backoffRef.current = 1000;
      heartbeatRef.current = setInterval(() => {
        if (ws.readyState === WebSocket.OPEN) ws.send('ping');
      }, HEARTBEAT_INTERVAL);
    };

    ws.onmessage = (e) => {
      if (!mountedRef.current) return;
      try {
        const event: WsEvent = JSON.parse(e.data);
        if (event.type) setLastEvent(event);
      } catch { /* ignore non-json */ }
    };

    ws.onclose = () => {
      if (!mountedRef.current) return;
      setStatus('disconnected');
      if (heartbeatRef.current) clearInterval(heartbeatRef.current);
      reconnectRef.current = setTimeout(() => {
        backoffRef.current = Math.min(backoffRef.current * 2, MAX_BACKOFF);
        connect();
      }, backoffRef.current);
    };

    ws.onerror = () => {
      ws.close();
    };
  }, [cleanup]);

  useEffect(() => {
    mountedRef.current = true;
    connect();
    return () => {
      mountedRef.current = false;
      cleanup();
    };
  }, [connect, cleanup]);

  const send = useCallback((data: unknown) => {
    if (wsRef.current?.readyState === WebSocket.OPEN) {
      wsRef.current.send(JSON.stringify(data));
    }
  }, []);

  return { status, lastEvent, send };
}
