var gifs = [
  {
    url: "/error_pages/request-denied.gif",
    caption: "Request... DENIED.",
  },
  {
    url: "/error_pages/nedry-magic-word.gif",
    caption: "Ah ah ah! You didn't say the magic word!",
  },
  {
    url: "/error_pages/sam-jackson-hacker.gif",
    caption: "God damn it! I hate this hacker crap!",
  },
  {
    url: "/error_pages/access-denied.gif",
    caption: "ACCESS DENIED. Changes locked out.",
  },
  {
    url: "/error_pages/nedry-smile.gif",
    caption: "Ah ah ah! You didn't say the magic word!",
  },
];

function loadRandomGif() {
  var pick = gifs[Math.floor(Math.random() * gifs.length)];
  var img = document.getElementById("gif-image");
  img.onerror = function () {
    loadRandomGif();
  };
  img.src = pick.url;
  document.getElementById("gif-caption").textContent = pick.caption;
}

window.onload = loadRandomGif;
