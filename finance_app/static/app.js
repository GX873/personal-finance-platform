"use strict";
document.addEventListener("keydown", (event) => {
  if (event.key === "Tab") document.documentElement.classList.add("keyboard-user");
}, { once: true });
