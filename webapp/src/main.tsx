import { StrictMode } from "react";
import { createRoot } from "react-dom/client";
import { tg } from "./telegram";
import App from "./App";
import "./styles.css";

const w = tg();
w?.ready();
w?.expand();
if (w?.colorScheme) document.documentElement.dataset.theme = w.colorScheme;

createRoot(document.getElementById("root")!).render(
  <StrictMode>
    <App />
  </StrictMode>,
);
