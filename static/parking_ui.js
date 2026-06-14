function plateColorLabel(color) {
    if (!color || color === "unknown") return "—";
    const text = String(color);
    return text.charAt(0).toUpperCase() + text.slice(1);
}
