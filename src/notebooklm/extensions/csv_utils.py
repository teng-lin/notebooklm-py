#!/usr/bin/env python3
"""
CSV File Management Tool
For reading, updating, and managing video processing progress
"""

import csv
import logging
from collections import defaultdict
from pathlib import Path

logger = logging.getLogger(__name__)


class ProgressManager:
    """Progress Manager"""

    def __init__(self, csv_path: Path):
        """
        Initialize progress manager

        Args:
            csv_path: CSV file path
        """
        self.csv_path = Path(csv_path)
        self.fieldnames = [
            'channel_name',
            'youtube_id',
            'youtube_title',
            'uptime',
            'status',
            'output_file'
        ]

        # 确保 CSV 文件存在
        if not self.csv_path.exists():
            self._create_empty_csv()

    def _create_empty_csv(self):
        """Create empty CSV file"""
        self.csv_path.parent.mkdir(parents=True, exist_ok=True)
        with open(self.csv_path, 'w', encoding='utf-8-sig', newline='') as f:
            writer = csv.DictWriter(f, fieldnames=self.fieldnames)
            writer.writeheader()
        logger.info(f"Created new CSV file: {self.csv_path}")

    def read_all(self) -> list[dict]:
        """
        Read all video records

        Returns:
            List of video records
        """
        videos = []
        try:
            with open(self.csv_path, encoding='utf-8-sig', newline='') as f:
                reader = csv.DictReader(f)
                for row in reader:
                    videos.append(row)
            logger.info(f"Read {len(videos)} video records")
        except Exception as e:
            logger.error(f"Failed to read CSV file: {e}")
            raise
        return videos

    def get_by_status(self, status: str) -> list[dict]:
        """
        Filter videos by status

        Args:
            status: Status (pending, processing, completed, failed)

        Returns:
            List of videos with matching status
        """
        all_videos = self.read_all()
        filtered = [v for v in all_videos if v.get('status') == status]
        logger.info(f"Found {len(filtered)} records with status '{status}'")
        return filtered

    def get_pending_videos(self) -> list[dict]:
        """Get pending videos (including pending, failed, and empty status)"""
        all_videos = self.read_all()
        # Get all videos with non-completed status
        pending = [v for v in all_videos if v.get('status') != 'completed']
        logger.info(f"Found {len(pending)} pending records (non-completed status)")
        return pending

    def group_by_channel(self, videos: list[dict] | None = None) -> dict[str, list[dict]]:
        """
        Group videos by channel

        Args:
            videos: Video list, if None reads all videos

        Returns:
            Dict of channel_name -> video list
        """
        if videos is None:
            videos = self.read_all()

        grouped = defaultdict(list)
        for video in videos:
            channel = video.get('channel_name', 'unknown')
            grouped[channel].append(video)

        logger.info(f"Videos grouped by {len(grouped)} channels")
        return dict(grouped)

    def update_status(self, youtube_id: str, status: str, output_file: str = ""):
        """
        Update status of a single video

        Args:
            youtube_id: Video ID
            status: New status
            output_file: Output filename (optional)
        """
        videos = self.read_all()
        updated = False

        for video in videos:
            if video['youtube_id'] == youtube_id:
                video['status'] = status
                if output_file:
                    video['output_file'] = output_file
                updated = True
                logger.info(f"Updated video {youtube_id} status to '{status}'")
                break

        if updated:
            self._write_all(videos)
        else:
            logger.warning(f"Video ID not found: {youtube_id}")

    def _write_all(self, videos: list[dict]):
        """
        Write all video records

        Args:
            videos: List of video records
        """
        try:
            with open(self.csv_path, 'w', encoding='utf-8-sig', newline='') as f:
                writer = csv.DictWriter(f, fieldnames=self.fieldnames)
                writer.writeheader()
                writer.writerows(videos)
            logger.debug(f"Successfully wrote {len(videos)} records to CSV")
        except Exception as e:
            logger.error(f"Failed to write CSV file: {e}")
            raise

    def add_video(self, channel_name: str, youtube_id: str, youtube_title: str,
                  uptime: str, status: str = "pending"):
        """
        Add new video record

        Args:
            channel_name: Channel name
            youtube_id: Video ID
            youtube_title: Video title
            uptime: Upload date
            status: Initial status
        """
        videos = self.read_all()

        # Check if already exists
        existing_ids = [v['youtube_id'] for v in videos]
        if youtube_id in existing_ids:
            logger.warning(f"Video {youtube_id} already exists, skipping")
            return

        new_video = {
            'channel_name': channel_name,
            'youtube_id': youtube_id,
            'youtube_title': youtube_title,
            'uptime': uptime,
            'status': status,
            'output_file': ''
        }

        videos.append(new_video)
        self._write_all(videos)
        logger.info(f"Added new video: {youtube_title} ({youtube_id})")

    def get_statistics(self) -> dict[str, int]:
        """
        Get processing statistics

        Returns:
            Status statistics dictionary
        """
        videos = self.read_all()
        stats = defaultdict(int)

        for video in videos:
            status = video.get('status', 'unknown')
            stats[status] += 1

        stats['total'] = len(videos)
        return dict(stats)
