const feed = document.querySelector("#feed");
const template = document.querySelector("#post-template");
const feedStatus = document.querySelector("#feed-status");

function formatDate(value, timeZone) {
  if (!value) return "时间未知";
  const date = new Date(value);
  if (Number.isNaN(date.getTime())) return value;
  try {
    return new Intl.DateTimeFormat("zh-CN", {
      year: "numeric",
      month: "2-digit",
      day: "2-digit",
      hour: "2-digit",
      minute: "2-digit",
      hour12: false,
      timeZone,
    }).format(date);
  } catch {
    return value;
  }
}

function formatCoordinate(value) {
  return Number.isFinite(Number(value)) ? Number(value).toFixed(5) : "?";
}

function renderPost(post) {
  const node = template.content.cloneNode(true);
  const image = node.querySelector(".post-image");
  const imageLink = node.querySelector(".image-link");
  const dynamicLink = node.querySelector(".dynamic-link");
  const summary = node.querySelector(".post-summary");
  const coordinateLink = node.querySelector(".coordinate-link");
  const locationLabel = node.querySelector(".location-label");
  const localTime = node.querySelector(".local-time");
  const latitude = formatCoordinate(post.latitude);
  const longitude = formatCoordinate(post.longitude);
  const beijingTime = formatDate(post.publishedAtBeijing, "Asia/Shanghai");

  image.src = post.image;
  image.alt = `${post.location || "世界某地"}凌晨四点的四时小路`;
  imageLink.href = post.image;
  image.addEventListener("error", () => {
    image.alt = "图片暂时无法加载";
    imageLink.classList.add("image-missing");
  });

  if (post.bilibiliDynamicUrl) {
    dynamicLink.href = post.bilibiliDynamicUrl;
    dynamicLink.textContent = `原动态 ↗（北京时间 ${beijingTime}）`;
  } else {
    dynamicLink.remove();
  }

  summary.textContent = post.bilibiliSummary || "";
  coordinateLink.textContent = `坐标 ${latitude}, ${longitude}`;
  coordinateLink.href = `https://www.openstreetmap.org/?mlat=${encodeURIComponent(latitude)}&mlon=${encodeURIComponent(longitude)}#map=16/${encodeURIComponent(latitude)}/${encodeURIComponent(longitude)}`;
  locationLabel.textContent = post.location || "地区未知";
  localTime.textContent = `当地时间 ${formatDate(post.localTime, post.timezone)}`;
  if (post.localTime) {
    localTime.dateTime = post.localTime;
  }
  return node;
}

async function loadPosts() {
  try {
    const response = await fetch("data/posts.json", { cache: "no-store" });
    if (!response.ok) throw new Error(`HTTP ${response.status}`);
    const payload = await response.json();
    const posts = Array.isArray(payload.posts) ? [...payload.posts] : [];
    posts.sort((left, right) =>
      String(right.publishedAtBeijing || "").localeCompare(
        String(left.publishedAtBeijing || ""),
      ),
    );
    feed.replaceChildren();
    if (posts.length === 0) {
      feedStatus.textContent = "0 条记录";
      return;
    }
    posts.forEach((post) => feed.append(renderPost(post)));
    feedStatus.textContent = `${posts.length} 条记录`;
  } catch (error) {
    const message = document.createElement("p");
    message.className = "error";
    message.textContent = `帖子加载失败：${error.message}`;
    feed.replaceChildren(message);
    feedStatus.textContent = "载入失败";
  }
}

loadPosts();
