import { defineConfig } from 'vite';
import react from '@vitejs/plugin-react';
import tailwindcss from '@tailwindcss/vite';

export default defineConfig({
  plugins: [react(), tailwindcss()],

  // Название вашего репозитория на GitHub для корректных путей на GitHub Pages.
  // Если переименуете репозиторий, замените 'wow_bro' на новое имя: base: '/<repo-name>/'
  base: '/wow_bro/',
});
