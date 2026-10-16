import { useCallback, useEffect, useRef, useState } from "react";

export default function useAudioLevel() {
  const [isActive, setIsActive] = useState(false);
  const [error, setError] = useState("");
  const levelRef = useRef({ level: 0, low: 0, mid: 0, high: 0 });
  const resources = useRef({ stream: null, context: null, frame: 0 });

  const stop = useCallback(() => {
    const current = resources.current;
    cancelAnimationFrame(current.frame);
    current.stream?.getTracks().forEach((track) => track.stop());
    if (current.context && current.context.state !== "closed") current.context.close();
    resources.current = { stream: null, context: null, frame: 0 };
    levelRef.current = { level: 0, low: 0, mid: 0, high: 0 };
    setIsActive(false);
  }, []);

  const start = useCallback(async () => {
    if (resources.current.stream) return true;
    setError("");
    try {
      if (!navigator.mediaDevices?.getUserMedia) throw new Error("Microphone access needed");
      const stream = await navigator.mediaDevices.getUserMedia({ audio: true });
      const context = new AudioContext();
      if (context.state === "suspended") await context.resume();
      const source = context.createMediaStreamSource(stream);
      const analyser = context.createAnalyser();
      analyser.fftSize = 256;
      analyser.smoothingTimeConstant = 0.8;
      source.connect(analyser);
      const samples = new Uint8Array(analyser.fftSize);
      const frequencies = new Uint8Array(analyser.frequencyBinCount);
      resources.current = { stream, context, frame: 0 };
      setIsActive(true);
      let smoothed = { level: 0, low: 0, mid: 0, high: 0 };
      const tick = () => {
        analyser.getByteTimeDomainData(samples);
        analyser.getByteFrequencyData(frequencies);
        let sum = 0;
        samples.forEach((sample) => { const value = (sample - 128) / 128; sum += value * value; });
        const bands = [0, 0, 0];
        const counts = [0, 0, 0];
        frequencies.forEach((value, index) => {
          const band = index < 8 ? 0 : index < 32 ? 1 : 2;
          bands[band] += value / 255;
          counts[band] += 1;
        });
        const raw = { level: Math.min(1, Math.sqrt(sum / samples.length) * 3.2), low: bands[0] / counts[0], mid: bands[1] / counts[1], high: bands[2] / counts[2] };
        smoothed = Object.fromEntries(Object.keys(raw).map((key) => [key, smoothed[key] + (raw[key] - smoothed[key]) * (raw[key] > smoothed[key] ? 0.35 : 0.08)]));
        levelRef.current = smoothed;
        resources.current.frame = requestAnimationFrame(tick);
      };
      resources.current.frame = requestAnimationFrame(tick);
      return true;
    } catch (cause) {
      stop();
      setError(cause?.name === "NotAllowedError" ? "Microphone access needed" : (cause?.message || "Microphone access needed"));
      return false;
    }
  }, [stop]);

  useEffect(() => stop, [stop]);
  return { start, stop, isActive, error, levelRef };
}
