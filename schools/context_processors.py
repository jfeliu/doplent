def current_school(request):
    return {"school": getattr(request, "school", None)}
