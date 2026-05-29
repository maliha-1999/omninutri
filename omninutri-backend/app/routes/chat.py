# omninutri-backend/app/routes/chat.py

import re
from typing import Any, Dict, Optional

from fastapi import APIRouter, UploadFile, File, Form

from app.agents.watcher import analyze_food_image
from app.agents.finder import analyze_meal_text
from app.agents.brain import generate_advice

router = APIRouter()

# NOTE: replace later with real per-user profile (DB/auth)
user_profile: Dict[str, Any] = {
    "age": 22,
    "height": 170,
    "weight": 65,
    "activity_level": "moderate",
    "goal": "muscle_gain",
    "budget": "medium",
}


def detect_intent(message: Optional[str]) -> Dict[str, Any]:
    t = (message or "").strip().lower()

    # Swagger often sends "string"
    if t == "string":
        t = ""

    recipe_words = [
        "recipe", "recepie", "receipe", "recipie",
        "how to make", "ingredients", "steps",
        "smoothie", "shake",
    ]
    suggest_words = [
        "suggest", "recommend", "options",
        "what should i eat", "meal plan", "budget", "next meal",
    ]
    food_log_words = [
        "i ate", "i had", "for breakfast", "for lunch", "for dinner",
        "snack", "today i ate", "it is",
    ]
    coaching_words = [
        "bmi", "calorie", "target", "water", "protein", "tdee",
        "progress", "how much left",
    ]

    return {
        "text": t,
        "want_recipe": any(w in t for w in recipe_words),
        "want_suggestions": any(w in t for w in suggest_words),
        "is_food_log": any(w in t for w in food_log_words),
        "is_coaching": any(w in t for w in coaching_words),
    }


def extract_servings_info(text: Optional[str]) -> str:
    """
    Pulls hints like:
      - "for 5 persons/people"
      - "8 pieces each/per plate/per person"
    and converts them into explicit instructions to help the model.
    """
    t = text or ""

    servings: Optional[int] = None
    pieces: Optional[int] = None

    m = re.search(r"\bfor\s+(\d+)\s*(people|persons|person|servings)?\b", t, re.I)
    if m:
        try:
            servings = int(m.group(1))
        except Exception:
            servings = None

    m2 = re.search(r"\b(\d+)\s*(pieces|piece)\s*(each|per person|per plate)?\b", t, re.I)
    if m2:
        try:
            pieces = int(m2.group(1))
        except Exception:
            pieces = None

    if not servings and not pieces:
        return ""

    extra = []
    if servings:
        extra.append(f"Make the recipe for {servings} people.")
    if servings and pieces:
        extra.append(f"Assume {pieces} pieces per person (total {servings * pieces} pieces).")
    elif pieces:
        extra.append(f"Assume {pieces} pieces per person.")

    return " ".join(extra)


def format_message(
    nutrition: Dict[str, Any],
    brain_out: Dict[str, Any],
    finder_out: Optional[Dict[str, Any]] = None,
) -> str:
    advice = brain_out.get("advice", "")

    def nget(k: str, default=0):
        v = nutrition.get(k, default)
        return v if v is not None else default

    lines = [
        f"🍽 Food: {nutrition.get('food_name', 'Unknown')}",
        f"📦 Quantity: {nutrition.get('estimated_quantity', '—')}",
        "",
        f"🔥 Calories: {nget('calories', 0)} kcal",
        (
            f"🥩 Protein: {nget('protein', 0)}g | "
            f"🍞 Carbs: {nget('carbohydrates', 0)}g | "
            f"🧈 Fat: {nget('fat', 0)}g | "
            f"🌾 Fiber: {nget('fiber', 0)}g"
        ),
        f"🎯 Confidence: {nget('confidence', 0)}",
        "",
        "💡 Advice:",
        advice,
    ]

    if finder_out:
        # ----- Recipe -----
        r = finder_out.get("recipe")
        if r:
            lines += [
                "",
                f"📌 Recipe: {r.get('name', 'Recipe')}",
                f"👥 Servings: {r.get('servings', 1)} | ⏱ Prep: {r.get('prep_minutes', 0)} min",
                "",
                "🧾 Ingredients:",
            ]

            for ing in (r.get("ingredients") or []):  # ✅ do NOT truncate
                lines.append(f"- {ing}")

            steps = r.get("steps") or []
            if steps:
                lines += ["", "👩‍🍳 Steps:"]
                for i, step in enumerate(steps, 1):
                    lines.append(f"{i}. {step}")

            tips = r.get("tips") or []
            if tips:
                lines += ["", "✅ Tips:"]
                for tip in tips:
                    lines.append(f"- {tip}")

        # ----- Suggestions -----
        suggestions = finder_out.get("suggestions") or []
        if suggestions:
            lines += ["", "🧠 Suggestions:"]
            for s in suggestions[:10]:
                lines.append(f"- {s}")

    return "\n".join(lines)


@router.post("/chat")
async def chat(message: str = Form(None), image: UploadFile = File(None)):
    intent = detect_intent(message)

    # -------------------------
    # IMAGE -> Watcher
    # -------------------------
    if image is not None and image.filename:
        image_bytes = await image.read()
        nutrition = analyze_food_image(image_bytes, image.content_type or "image/jpeg")

        # watcher clarification UI
        if nutrition.get("needs_clarification"):
            options = [
                a.get("food_name")
                for a in (nutrition.get("alternatives") or [])
                if a.get("food_name")
            ]
            return {
                "source": "watcher",
                "nutrition": nutrition,
                "message": nutrition.get("question") or "Please confirm the food.",
                "ui": {"type": "clarification", "options": options},
            }

        brain_out = generate_advice(user_profile, nutrition)

        finder_out = None
        if intent["want_recipe"] or intent["want_suggestions"]:
            # If user typed anything like "recipe for 5 people / 8 pieces each", pass it through.
            servings_hint = extract_servings_info(message)
            food = nutrition.get("food_name") or "this dish"

            user_text = message.strip() if (message or "").strip() else f"Give recipe/suggestions for {food}"
            if servings_hint:
                user_text = f"{user_text}\n\n{servings_hint}"

            # Ensure the model knows what food was detected from the image
            user_text = f"{user_text}\n\nFood detected from image: {food}"

            finder_out = analyze_meal_text(
                user_text=user_text,
                goal=user_profile["goal"],
                budget=user_profile["budget"],
                want_recipe=intent["want_recipe"],
                want_suggestions=intent["want_suggestions"],
            )

        return {
            "source": "watcher",
            "nutrition": nutrition,
            "health_profile": brain_out.get("health_profile"),
            "finder": finder_out,
            "message": format_message(nutrition, brain_out, finder_out),
        }

    # -------------------------
    # TEXT -> Finder or Brain
    # -------------------------
    if intent["text"]:
        # coaching-only -> brain
        if intent["is_coaching"] and not (
            intent["want_recipe"] or intent["want_suggestions"] or intent["is_food_log"]
        ):
            brain_out = generate_advice(user_profile, None)
            return {
                "source": "brain",
                "health_profile": brain_out.get("health_profile"),
                "message": brain_out.get("advice"),
            }

        # Add servings/pieces hints to improve recipe quality
        servings_hint = extract_servings_info(intent["text"])
        user_text_for_finder = intent["text"]
        if servings_hint:
            user_text_for_finder = f"{user_text_for_finder}\n\n{servings_hint}"

        finder_out = analyze_meal_text(
            user_text=user_text_for_finder,
            goal=user_profile["goal"],
            budget=user_profile["budget"],
            want_recipe=intent["want_recipe"],
            want_suggestions=intent["want_suggestions"],
        )

        nutrition = (finder_out or {}).get("nutrition") or {}
        brain_out = generate_advice(user_profile, nutrition)

        return {
            "source": "finder",
            "nutrition": nutrition,
            "health_profile": brain_out.get("health_profile"),
            "finder": finder_out,
            "message": format_message(nutrition, brain_out, finder_out),
        }

    return {"message": "Send a message or upload an image."}