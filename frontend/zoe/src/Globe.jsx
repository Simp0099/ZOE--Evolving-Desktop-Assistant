import { useEffect, useRef } from "react";

export default function Globe({ mode = "idle", levelRef, speakingLevelRef }) {
  const wrapperRef = useRef(null);

  useEffect(() => {
    let frame = 0;
    let lastPeak = 0;
    let fakePhase = 0;
    const node = wrapperRef.current;
    const tick = (time) => {
      if (!node) return;
      let data = levelRef?.current || { level: 0, low: 0, mid: 0, high: 0 };
      if (mode === "speaking") {
        if (speakingLevelRef?.current != null) {
          const level = Math.max(0, Math.min(1, speakingLevelRef.current));
          data = { level, low: level * 0.8, mid: level, high: level * 0.65 };
        } else {
          fakePhase += 0.018;
          const level = 0.12 + Math.max(0, Math.sin(fakePhase * 2.3)) * 0.3 + Math.max(0, Math.sin(fakePhase * 5.1)) * 0.12;
          data = { level, low: level * 0.8, mid: level, high: level * 0.65 };
        }
      }
      const level = mode === "listening" || mode === "speaking" ? data.level : 0;
      node.style.setProperty("--level", level.toFixed(3));
      node.style.setProperty("--low", data.low.toFixed(3));
      node.style.setProperty("--mid", data.mid.toFixed(3));
      node.style.setProperty("--high", data.high.toFixed(3));
      node.style.setProperty("--globe-scale", String(1 + level * 0.18));
      if (mode === "listening" && level > 0.32 && time - lastPeak > 440) {
        node.classList.remove("globe-peak");
        void node.offsetWidth;
        node.classList.add("globe-peak");
        lastPeak = time;
      }
      frame = requestAnimationFrame(tick);
    };
    frame = requestAnimationFrame(tick);
    return () => cancelAnimationFrame(frame);
  }, [mode, levelRef, speakingLevelRef]);

  return <div ref={wrapperRef} className={`hero-globe globe-mode-${mode}`} role="img" aria-label={`Zoe globe ${mode}`}>
    <div className="globe-orbit orbit-one"><i /></div><div className="globe-orbit orbit-two"><i /></div>
    <div className="globe-ripple ripple-one" /><div className="globe-ripple ripple-two" /><div className="globe-ripple ripple-three" />
    <img className="globe-image" src="/globe.png" alt="" />
  </div>;
}
