import { useEffect, useRef } from 'react';

const COLORS = [
  '#e879f9', // fuchsia-400
  '#a855f7', // purple-500
  '#34d399', // emerald-400
  '#facc15', // yellow-400
  '#38bdf8', // sky-400
  '#fb7185', // rose-400
  '#c084fc', // purple-400
];

function createParticles(width, height, count = 130) {
  const particles = [];
  for (let i = 0; i < count; i += 1) {
    const fromLeft = i % 3 === 0;
    const fromRight = i % 3 === 1;
    let x;
    let y;
    let vx;
    let vy;

    if (fromLeft) {
      x = width * 0.08;
      y = height * 0.65;
      vx = 4 + Math.random() * 9;
      vy = -(10 + Math.random() * 10);
    } else if (fromRight) {
      x = width * 0.92;
      y = height * 0.65;
      vx = -(4 + Math.random() * 9);
      vy = -(10 + Math.random() * 10);
    } else {
      x = Math.random() * width;
      y = -20 - Math.random() * height * 0.35;
      vx = (Math.random() - 0.5) * 4;
      vy = 2 + Math.random() * 4;
    }

    particles.push({
      x,
      y,
      vx,
      vy,
      size: 6 + Math.random() * 7,
      color: COLORS[Math.floor(Math.random() * COLORS.length)],
      rotation: Math.random() * Math.PI * 2,
      rotationSpeed: (Math.random() - 0.5) * 0.25,
      tilt: Math.random() * Math.PI,
      tiltSpeed: 0.06 + Math.random() * 0.1,
      shape: Math.random() > 0.35 ? 'rect' : 'circle',
      opacity: 1,
    });
  }
  return particles;
}

export default function ConfettiCanvas({ burstKey = 0 }) {
  const canvasRef = useRef(null);

  useEffect(() => {
    const canvas = canvasRef.current;
    if (!canvas) return undefined;
    const ctx = canvas.getContext('2d');
    if (!ctx) return undefined;

    let animationFrameId;
    const resize = () => {
      canvas.width = window.innerWidth;
      canvas.height = window.innerHeight;
    };
    resize();
    window.addEventListener('resize', resize);

    const particles = createParticles(canvas.width, canvas.height, 140);
    let frame = 0;

    const render = () => {
      frame += 1;
      ctx.clearRect(0, 0, canvas.width, canvas.height);

      let alive = 0;
      for (let i = 0; i < particles.length; i += 1) {
        const p = particles[i];
        p.vy += 0.24; // gravity
        p.vx *= 0.992; // air drag
        p.x += p.vx;
        p.y += p.vy;
        p.rotation += p.rotationSpeed;
        p.tilt += p.tiltSpeed;

        if (frame > 130) {
          p.opacity = Math.max(0, p.opacity - 0.012);
        }

        if (p.opacity > 0.01 && p.y < canvas.height + 40) {
          alive += 1;
          ctx.save();
          ctx.globalAlpha = p.opacity;
          ctx.translate(p.x, p.y);
          ctx.rotate(p.rotation);
          ctx.fillStyle = p.color;

          if (p.shape === 'rect') {
            const scaleY = Math.cos(p.tilt);
            ctx.fillRect(-p.size / 2, (-p.size * scaleY) / 2, p.size, p.size * 0.55 * Math.max(0.25, Math.abs(scaleY)));
          } else {
            ctx.beginPath();
            ctx.arc(0, 0, p.size * 0.4, 0, Math.PI * 2);
            ctx.fill();
          }
          ctx.restore();
        }
      }

      if (alive > 0) {
        animationFrameId = window.requestAnimationFrame(render);
      } else {
        ctx.clearRect(0, 0, canvas.width, canvas.height);
      }
    };

    animationFrameId = window.requestAnimationFrame(render);

    return () => {
      window.removeEventListener('resize', resize);
      window.cancelAnimationFrame(animationFrameId);
    };
  }, [burstKey]);

  return (
    <canvas
      ref={canvasRef}
      data-testid="confetti-canvas"
      aria-hidden="true"
      className="pointer-events-none fixed inset-0 z-50 h-full w-full"
    />
  );
}
