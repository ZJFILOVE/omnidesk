import React, { useEffect, useRef } from 'react';
import {
  MPV_FRAME_CHANNEL,
  isTauriEventsAvailable,
  listenTauriEvent,
  mpvEmbedSetViewport,
  type MpvFrameEnvelope,
  type TauriUnlistenFn,
} from '../api';

export interface MpvCanvasProps {
  active: boolean;
  fps?: number;
  className?: string;
}

function isFrameEnvelope(payload: unknown): payload is MpvFrameEnvelope {
  if (typeof payload !== 'object' || payload === null) return false;
  const frame = payload as Partial<MpvFrameEnvelope>;
  return (
    typeof frame.seq === 'number' &&
    typeof frame.width === 'number' &&
    typeof frame.height === 'number' &&
    typeof frame.mime === 'string' &&
    typeof frame.data === 'string' &&
    frame.data.length > 0
  );
}

function decodeBase64(data: string): ArrayBuffer {
  const binary = atob(data);
  const buffer = new ArrayBuffer(binary.length);
  const bytes = new Uint8Array(buffer);
  for (let i = 0; i < binary.length; i += 1) {
    bytes[i] = binary.charCodeAt(i);
  }
  return buffer;
}

async function decodeFrame(frame: MpvFrameEnvelope): Promise<CanvasImageSource> {
  const mime = frame.mime || 'image/jpeg';
  if (typeof createImageBitmap === 'function') {
    let blob: Blob;
    if (typeof fetch === 'function' && frame.data.length > 1024) {
      try {
        const response = await fetch(`data:${mime};base64,${frame.data}`);
        blob = response.ok ? await response.blob() : new Blob([decodeBase64(frame.data)], { type: mime });
      } catch {
        blob = new Blob([decodeBase64(frame.data)], { type: mime });
      }
    } else {
      blob = new Blob([decodeBase64(frame.data)], { type: mime });
    }
    try {
      return await createImageBitmap(blob);
    } catch {
      // Fall through to the <img> decoder used by older WebKitGTK builds.
    }
  }
  return await new Promise<HTMLImageElement>((resolve, reject) => {
    const image = new Image();
    image.onload = () => resolve(image);
    image.onerror = () => reject(new Error('JPEG frame decode failed'));
    image.src = `data:${mime};base64,${frame.data}`;
  });
}

export const MpvCanvas: React.FC<MpvCanvasProps> = ({ active, fps = 30, className }) => {
  const canvasRef = useRef<HTMLCanvasElement | null>(null);
  const lastSeqRef = useRef(0);
  const viewportTimerRef = useRef<number | null>(null);

  useEffect(() => {
    if (!active) return undefined;
    let disposed = false;
    let unlisten: TauriUnlistenFn | null = null;
    let decoding = false;
    let pending: MpvFrameEnvelope | null = null;
    lastSeqRef.current = 0;

    const drawFrame = async (frame: MpvFrameEnvelope) => {
      if (disposed || frame.seq <= lastSeqRef.current) return;
      if (decoding) { if (!pending || frame.seq > pending.seq) pending = frame; return; }
      lastSeqRef.current = frame.seq;
      decoding = true;
      try {
        const bitmap = await decodeFrame(frame);
        const canvas = canvasRef.current;
        if (canvas && !disposed) {
          if (canvas.width !== frame.width || canvas.height !== frame.height) {
            canvas.width = frame.width;
            canvas.height = frame.height;
          }
          const ctx = canvas.getContext('2d');
          if (ctx) {
            ctx.drawImage(bitmap, 0, 0, canvas.width, canvas.height);
          }
        }
        if (typeof (bitmap as ImageBitmap).close === 'function') {
          (bitmap as ImageBitmap).close();
        }
      } catch {
        // A dropped frame is preferable to blocking the event stream.
      } finally {
        decoding = false;
        const next = pending;
        pending = null;
        if (next && !disposed) void drawFrame(next);
      }
    };

    if (isTauriEventsAvailable()) {
      void listenTauriEvent<unknown>(MPV_FRAME_CHANNEL, (payload) => {
        if (isFrameEnvelope(payload)) {
          void drawFrame(payload);
        }
      }).then((off) => {
        if (disposed) {
          if (off) void off();
        } else {
          unlisten = off;
        }
      });
    }

    return () => {
      disposed = true;
      if (unlisten) void unlisten();
    };
  }, [active]);

  useEffect(() => {
    if (!active) return undefined;
    const canvas = canvasRef.current;
    if (!canvas) return undefined;

    const reportViewport = () => {
      const rect = canvas.getBoundingClientRect();
      if (rect.width < 1 || rect.height < 1) return;
      const width = Math.round(rect.width);
      const height = Math.round(rect.height);
      if (viewportTimerRef.current !== null) {
        window.clearTimeout(viewportTimerRef.current);
      }
      viewportTimerRef.current = window.setTimeout(() => {
        viewportTimerRef.current = null;
        void mpvEmbedSetViewport(width, height, Number.isFinite(fps) && fps > 0 ? Math.min(60, Math.ceil(fps)) : 30);
      }, 120);
    };

    reportViewport();
    if (typeof ResizeObserver === 'undefined') {
      window.addEventListener('resize', reportViewport);
      return () => {
        window.removeEventListener('resize', reportViewport);
        if (viewportTimerRef.current !== null) {
          window.clearTimeout(viewportTimerRef.current);
          viewportTimerRef.current = null;
        }
      };
    }

    const observer = new ResizeObserver(reportViewport);
    observer.observe(canvas);
    return () => {
      observer.disconnect();
      if (viewportTimerRef.current !== null) {
        window.clearTimeout(viewportTimerRef.current);
        viewportTimerRef.current = null;
      }
    };
  }, [active, fps]);

  return (
    <canvas
      ref={canvasRef}
      aria-label="嵌入式 mpv 视频画布"
      data-testid="mpv-canvas"
      width={960}
      height={540}
      className={className ?? 'h-full w-full bg-black'}
    />
  );
};

export default MpvCanvas;
