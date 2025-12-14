class IncidentDetector:
    """
    Placeholder deterministic incident detector.
    You can extend it later to rank hypotheses based on evidence patterns.
    """
    def detect(self, evidence):
        return {
            "phase": "investigating",
            "symptoms": [],
            "suspected_domain": None,
            "confidence": 0.0,
        }
