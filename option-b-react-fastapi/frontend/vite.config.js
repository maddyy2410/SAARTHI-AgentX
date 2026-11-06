// vite.config.js : settings for Vite, the tool that runs and builds the React app
import { defineConfig } from "vite"; // helper that gives nice autocomplete for settings
import react from "@vitejs/plugin-react"; // teaches Vite to understand React's JSX syntax

export default defineConfig({ // export the settings so Vite can read them
  plugins: [react()], // turn on the React plugin
  server: { port: 5173 }, // the app opens at http://localhost:5173
});
