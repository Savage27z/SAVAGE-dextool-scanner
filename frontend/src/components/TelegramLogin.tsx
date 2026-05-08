import { useEffect, useRef } from 'react';
import { API_URL } from '../api/client';

const BOT_NAME = import.meta.env.VITE_TELEGRAM_BOT_NAME || 'your_bot_name';

export default function TelegramLogin() {
  const containerRef = useRef<HTMLDivElement>(null);

  useEffect(() => {
    const script = document.createElement('script');
    script.src = 'https://telegram.org/js/telegram-widget.js?22';
    script.setAttribute('data-telegram-login', BOT_NAME);
    script.setAttribute('data-size', 'large');
    script.setAttribute('data-radius', '8');
    script.setAttribute('data-auth-url', `${API_URL}/api/auth/telegram/callback`);
    script.setAttribute('data-request-access', 'write');
    script.async = true;

    containerRef.current?.appendChild(script);
  }, []);

  return (
    <div className="flex flex-col items-center gap-4">
      <div ref={containerRef} className="flex justify-center" />
      <p className="text-text-muted text-xs text-center leading-relaxed max-w-[280px]">
        After Telegram says it sent a message, open Telegram and confirm the login.
        If no message arrives, open/start the bot in Telegram and retry.
      </p>
    </div>
  );
}
