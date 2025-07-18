import boto3
import re
import random
import time
import json
import os
import logging
from botocore.exceptions import ClientError
from IPython.display import HTML
from base64 import b64encode
from IPython.display import Audio, display

suffix = random.randrange(200, 900)
boto3_session = boto3.session.Session()
region_name = boto3_session.region_name
iam_client = boto3_session.client("iam")
s3_client = boto3_session.client("s3")
account_number = boto3.client("sts").get_caller_identity().get("Account")
identity = boto3.client("sts").get_caller_identity()["Arn"]

bedrock_agent_client = boto3.client("bedrock-agent")


def interactive_sleep(seconds: int):
    dots = ""
    for i in range(seconds):
        dots += "."
        print(dots, end="\r")
        time.sleep(1)


def print_results(kb_response, response):
    # Print the KB retrieval results
    print("Knowledge Base retrieval results:\n")
    for i, result in enumerate(kb_response["retrievalResults"], start=1):
        text = result["content"]["text"]
        text = re.sub(r"\s+", " ", text)
        print(f"Chunk {i}:\n{text}\n")

    # Print the text
    print("MODEL RESPONSE:\n")
    print(response["output"]["message"]["content"][0]["text"])


def print_results_with_guardrail(kb_response, response):
    # Print the KB retrieval results
    print("Knowledge Base retrieval results:\n")
    for i, result in enumerate(kb_response["retrievalResults"], start=1):
        text = result["content"]["text"]
        text = re.sub(r"\s+", " ", text)
        print(f"Chunk {i}:\n{text}\n")

    # Print the text
    print("MODEL RESPONSE:\n")
    print(response["output"]["message"]["content"][0]["text"])

    # Print the outputAssessments scores
    print("\nCONTEXTUAL GROUNDING SCORES:\n")
    for key, assessments in response["trace"]["guardrail"]["outputAssessments"].items():
        for assessment in assessments:
            for filter in assessment["contextualGroundingPolicy"]["filters"]:
                print(
                    f"Filter type: {filter['type']}, Score: {filter['score']}, Threshold: {filter['threshold']}, Passed: {filter['score'] >= filter['threshold']}"
                )

    if response["stopReason"] == "guardrail_intervened":
        print("\nGuardrail intervened")
        print(
            "Model final response ->",
            response["output"]["message"]["content"][0]["text"],
        )
        print(
            "Model response ->",
            json.dumps(
                json.loads(response["trace"]["guardrail"]["modelOutput"][0]), indent=2
            ),
        )


from typing import List, Dict, Union


# Function to create document config to ingest document into a Bedrock Knowledge Base using DLA
def create_document_config(
    data_source_type: str,
    document_id: str = None,
    s3_uri: str = None,
    inline_content: Dict = None,
    metadata: Union[List[Dict], Dict] = None,
) -> Dict:
    """
    Create a document configuration for ingestion.

    :param data_source_type: Either 'CUSTOM' or 'S3'.
    :param document_id: The ID for a custom document.
    :param s3_uri: The S3 URI for S3 data source.
    :param inline_content: The inline content configuration for custom data source.
    :param metadata: Metadata for the document. Can be a list of inline attributes or an S3 location.
    :return: A document configuration dictionary.
    """
    document = {"content": {"dataSourceType": data_source_type}}

    if data_source_type == "CUSTOM":
        document["content"]["custom"] = {
            "customDocumentIdentifier": {"id": document_id},
            "sourceType": "IN_LINE" if inline_content else "S3_LOCATION",
        }
        if inline_content:
            content_type = inline_content.get("type", "TEXT")
            document["content"]["custom"]["inlineContent"] = {"type": content_type}
            if content_type == "BYTE":
                document["content"]["custom"]["inlineContent"]["byteContent"] = {
                    "data": inline_content["data"],
                    "mimeType": inline_content["mimeType"],
                }
            else:  # TEXT
                document["content"]["custom"]["inlineContent"]["textContent"] = {
                    "data": inline_content["data"]
                }
        elif s3_uri:
            document["content"]["custom"]["s3Location"] = {"uri": s3_uri}
    elif data_source_type == "S3":
        document["content"]["s3"] = {"s3Location": {"uri": s3_uri}}

    if metadata:
        if isinstance(metadata, list):
            document["metadata"] = {
                "type": "IN_LINE_ATTRIBUTE",
                "inlineAttributes": metadata,
            }
        elif isinstance(metadata, dict) and "uri" in metadata:
            document["metadata"] = {
                "type": "S3_LOCATION",
                "s3Location": {
                    "uri": metadata["uri"],
                    "bucketOwnerAccountId": metadata.get("bucketOwnerAccountId"),
                },
            }
            if (
                "bucketOwnerAccountId" in document["metadata"]["s3Location"]
                and document["metadata"]["s3Location"]["bucketOwnerAccountId"] is None
            ):
                del document["metadata"]["s3Location"]["bucketOwnerAccountId"]

    return document


# Function to to ingest document into a Bedrock Knowledge Base using DLA


def ingest_documents_dla(
    knowledge_base_id: str,
    data_source_id: str,
    documents: List[Dict[str, Union[Dict, str]]],
    client_token: str = None,
) -> Dict:
    """
    Ingest documents into a knowledge base using the Amazon Bedrock API.

    :param knowledge_base_id: The ID of the knowledge base.
    :param data_source_id: The ID of the data source.
    :param documents: A list of document configurations to ingest.
    :param client_token: Optional unique token for request idempotency.
    :return: The API response.
    """
    bedrock_agent_client = boto3.client("bedrock-agent")

    request = {
        "knowledgeBaseId": knowledge_base_id,
        "dataSourceId": data_source_id,
        "documents": documents,
    }

    if client_token:
        request["clientToken"] = client_token

    return bedrock_agent_client.ingest_knowledge_base_documents(**request)


def create_kedra_genai_index_role(kendra_role_name, bucket_name, account_id):
    kendra_policy_document = {
        "Version": "2012-10-17",
        "Statement": [
            {
                "Effect": "Allow",
                "Action": ["cloudwatch:PutMetricData"],
                "Resource": "*",
                "Condition": {"StringEquals": {"cloudwatch:namespace": "AWS/Kendra"}},
            },
            {"Effect": "Allow", "Action": ["logs:DescribeLogGroups"], "Resource": "*"},
            {
                "Effect": "Allow",
                "Action": ["logs:CreateLogGroup"],
                "Resource": [
                    f"arn:aws:logs:{region_name}:{account_id}:log-group:/aws/kendra/*"
                ],
            },
            {
                "Effect": "Allow",
                "Action": [
                    "logs:DescribeLogStreams",
                    "logs:CreateLogStream",
                    "logs:PutLogEvents",
                ],
                "Resource": [
                    f"arn:aws:logs:{region_name}:{account_id}:log-group:/aws/kendra/*:log-stream:*"
                ],
            },
        ],
    }

    s3_policy_document = {
        "Version": "2012-10-17",
        "Statement": [
            {
                "Effect": "Allow",
                "Action": ["s3:GetObject", "s3:ListBucket"],
                "Resource": [
                    f"arn:aws:s3:::{bucket_name}",
                    f"arn:aws:s3:::{bucket_name}/*",
                ],
                "Condition": {"StringEquals": {"aws:ResourceAccount": f"{account_id}"}},
            }
        ],
    }

    assume_role_policy_document = {
        "Version": "2012-10-17",
        "Statement": [
            {
                "Effect": "Allow",
                "Principal": {"Service": "kendra.amazonaws.com"},
                "Action": "sts:AssumeRole",
            }
        ],
    }

    # create policies based on the policy documents
    s3_policy = iam_client.create_policy(
        PolicyName="s3_permissions",
        PolicyDocument=json.dumps(s3_policy_document),
        Description="Policy for kendra to access and write to s3 bucket",
    )

    kendra_policy = iam_client.create_policy(
        PolicyName="kendra_permissions",
        PolicyDocument=json.dumps(kendra_policy_document),
        Description="Policy for kendra to access and write to cloudwatch",
    )

    # create Kendra Gen AI Index role
    kendra_genai_index_role = iam_client.create_role(
        RoleName=kendra_role_name,
        AssumeRolePolicyDocument=json.dumps(assume_role_policy_document),
        Description="Role for Kendra Gen AI Index",
        MaxSessionDuration=3600,
    )

    # fetch arn of the policies and role created above
    kendra_genai_index_role_arn = kendra_genai_index_role["Role"]["Arn"]
    s3_policy_arn = s3_policy["Policy"]["Arn"]
    kendra_policy_arn = kendra_policy["Policy"]["Arn"]

    # attach policies to Kendra Gen AI Index role
    iam_client.attach_role_policy(RoleName=kendra_role_name, PolicyArn=s3_policy_arn)

    iam_client.attach_role_policy(
        RoleName=kendra_role_name, PolicyArn=kendra_policy_arn
    )

    return kendra_genai_index_role


# create s3 bucket


def create_bucket(bucket_name, region=None):
    """Create an S3 bucket in a specified region

    If a region is not specified, the bucket is created in the S3 default
    region (us-east-1).

    :param bucket_name: Bucket to create
    :param region: String region to create bucket in, e.g., 'us-west-2'
    :return: True if bucket created, else False
    """

    # Create bucket
    try:
        if region is None:
            s3_client = boto3.client("s3")
            resp = s3_client.create_bucket(Bucket=bucket_name)
        else:
            s3_client = boto3.client("s3", region_name=region)
            location = {"LocationConstraint": region}
            s3_client.create_bucket(
                Bucket=bucket_name, CreateBucketConfiguration=location
            )
    except ClientError as e:
        logging.error(e)
        return False
    return resp


# upload data to s3


def upload_to_s3(path, bucket_name):
    for root, dirs, files in os.walk(path):
        for file in files:
            file_to_upload = os.path.join(root, file)
            print(f"uploading file {file_to_upload} to {bucket_name}")
            s3_client.upload_file(file_to_upload, bucket_name, file)


# check if bucket exist
def bucket_exists(bucket_name):
    try:
        s3_client.head_bucket(Bucket=bucket_name)
        return True
    except ClientError as e:
        if e.response["Error"]["Code"] == "404":
            return False
        else:
            raise e  # Raise other unexpected errors


def play(filename):
    html = ""
    video = open(filename, "rb").read()
    src = "data:video/mp4;base64," + b64encode(video).decode()
    html += (
        '<video width=1000 controls autoplay loop><source src="%s" type="video/mp4"></video>'
        % src
    )
    return HTML(html)


def extract_audio_path_and_timestamps(response):
    timestamps = []
    audio_s3_info = None

    try:
        if "citations" in response:
            for citation in response["citations"]:
                if "retrievedReferences" in citation:
                    for ref in citation["retrievedReferences"]:
                        # Check for the new metadata structure
                        if "metadata" in ref:
                            metadata = ref["metadata"]
                            if "x-amz-bedrock-kb-source-uri" in metadata:
                                s3_uri = metadata["x-amz-bedrock-kb-source-uri"]
                                # Parse s3 URI to get bucket and key
                                if s3_uri.startswith("s3://"):
                                    parts = s3_uri[5:].split("/", 1)
                                    if len(parts) == 2:
                                        audio_s3_info = {
                                            "bucket": parts[0],
                                            "key": parts[1],
                                        }

                        # Extract timestamps if present in content
                        if "content" in ref and "text" in ref["content"]:
                            content_text = ref["content"]["text"]

                            # Updated regex patterns to match the actual format
                            timestamp_pattern = r'"start_timestamp_millis":\s*(\d+),\s*"end_timestamp_millis":\s*(\d+),\s*"segment_index":\s*(\d+),\s*"type":\s*"TRANSCRIPT",\s*"text":\s*"([^"]+)"'

                            # Find all matches with the new pattern
                            matches = re.findall(timestamp_pattern, content_text)

                            # Process matches
                            for match in matches:
                                start, end, index, text = match
                                timestamps.append(
                                    {
                                        "start": int(start),
                                        "end": int(end),
                                        "segment_index": int(index),
                                        "text": text,
                                        "speaker": "Speaker",  # Default speaker if not available
                                    }
                                )

    except Exception as e:
        print(f"Error in main processing: {e}")

    # Sort timestamps by segment_index
    if timestamps:
        timestamps.sort(key=lambda x: x["segment_index"])

    return audio_s3_info, timestamps


def play_audio_segment(audio_s3_info, start_ms, end_ms=None):
    """
    Play the audio segment using IPython.display.Audio from S3:
    1. First fetch the JSON file containing metadata
    2. Extract the actual MP3 file location from the JSON
    3. Fetch and play the MP3 file
    """
    if not audio_s3_info:
        print("No audio file information found in response")
        return

    try:
        # 1. First get the JSON file from S3
        s3_client = boto3.client("s3")
        json_response = s3_client.get_object(
            Bucket=audio_s3_info["bucket"], Key=audio_s3_info["key"]
        )
        json_content = json.loads(json_response["Body"].read().decode("utf-8"))

        # 2. Extract the actual MP3 file location from JSON metadata
        metadata = json_content.get("metadata", {})  # Get the metadata dictionary
        mp3_bucket = metadata.get("s3_bucket")
        mp3_key = metadata.get("s3_key")

        if not mp3_bucket or not mp3_key:
            print("MP3 file information not found in JSON metadata")
            return

        # 3. Get the actual MP3 file from S3
        if not hasattr(play_audio_segment, "audio_data"):
            try:
                mp3_response = s3_client.get_object(Bucket=mp3_bucket, Key=mp3_key)
                play_audio_segment.audio_data = mp3_response["Body"].read()
            except Exception as e:
                print(f"Error fetching MP3 from S3: {e}")
                return

        # Create audio object without autoplay
        audio = Audio(
            data=play_audio_segment.audio_data,
            autoplay=False,
            rate=metadata.get("sample_rate", 44100),  # Use sample rate from metadata
        )

        display(audio)

    except Exception as e:
        print(f"Error processing audio: {e}")
        return


def parse_response_and_get_s3_info(response):
    video_info = {"s3_uri": None, "timestamps": [], "summary": None, "transcript": None}

    try:
        # Parse citations
        if "citations" in response:
            for citation in response["citations"]:
                if "retrievedReferences" in citation:
                    for ref in citation["retrievedReferences"]:
                        try:
                            # Get S3 URI from metadata
                            if "metadata" in ref:
                                s3_uri = ref["metadata"].get(
                                    "x-amz-bedrock-kb-source-uri"
                                )
                                if s3_uri and not video_info["s3_uri"]:
                                    parts = s3_uri.replace("s3://", "").split("/", 1)
                                    if len(parts) == 2:
                                        video_info["s3_uri"] = {
                                            "bucket": parts[0],
                                            "key": parts[1],
                                        }

                            # Get content information
                            if "content" in ref:
                                content = ref["content"]
                                content_text = content.get("text", "")

                                # First try to find complete shots array
                                if '"shots": [' in content_text:
                                    try:
                                        shots_start = content_text.find('"shots": [')
                                        shots_start = content_text.find(
                                            "[", shots_start
                                        )
                                        if shots_start >= 0:
                                            # Find matching closing bracket
                                            bracket_count = 1
                                            shots_end = shots_start + 1
                                            while bracket_count > 0 and shots_end < len(
                                                content_text
                                            ):
                                                if content_text[shots_end] == "[":
                                                    bracket_count += 1
                                                elif content_text[shots_end] == "]":
                                                    bracket_count -= 1
                                                shots_end += 1

                                            if bracket_count == 0:
                                                shots_text = content_text[
                                                    shots_start:shots_end
                                                ]
                                                try:
                                                    shots_array = json.loads(shots_text)
                                                    for shot in shots_array:
                                                        if (
                                                            isinstance(shot, dict)
                                                            and "shot_index" in shot
                                                        ):
                                                            timestamp = {
                                                                "shot_index": shot.get(
                                                                    "shot_index"
                                                                ),
                                                                "start_time": shot.get(
                                                                    "start_timestamp_millis"
                                                                ),
                                                                "end_time": shot.get(
                                                                    "end_timestamp_millis"
                                                                ),
                                                                "start_timecode": shot.get(
                                                                    "start_timecode_smpte"
                                                                ),
                                                                "end_timecode": shot.get(
                                                                    "end_timecode_smpte"
                                                                ),
                                                                "duration": shot.get(
                                                                    "duration_millis"
                                                                ),
                                                            }
                                                            if (
                                                                timestamp["start_time"]
                                                                is not None
                                                            ):
                                                                video_info[
                                                                    "timestamps"
                                                                ].append(timestamp)
                                                except json.JSONDecodeError:
                                                    print(
                                                        f"Failed to parse shots array: {shots_text[:200]}"
                                                    )
                                    except Exception as e:
                                        print(f"Error processing shots array: {e}")

                                # Also look for individual shot objects
                                if (
                                    "shot_index" in content_text
                                    and "start_timestamp_millis" in content_text
                                ):
                                    try:
                                        # Find the complete shot object
                                        start_idx = content_text.find("{")
                                        end_idx = content_text.find("}", start_idx) + 1
                                        if start_idx >= 0 and end_idx > 0:
                                            shot_text = content_text[start_idx:end_idx]
                                            shot_json = json.loads(shot_text)

                                            if (
                                                "shot_index" in shot_json
                                                and "start_timestamp_millis"
                                                in shot_json
                                            ):
                                                timestamp = {
                                                    "shot_index": shot_json.get(
                                                        "shot_index"
                                                    ),
                                                    "start_time": shot_json.get(
                                                        "start_timestamp_millis"
                                                    ),
                                                    "end_time": shot_json.get(
                                                        "end_timestamp_millis"
                                                    ),
                                                    "start_timecode": shot_json.get(
                                                        "start_timecode_smpte"
                                                    ),
                                                    "end_timecode": shot_json.get(
                                                        "end_timecode_smpte"
                                                    ),
                                                    "duration": shot_json.get(
                                                        "duration_millis"
                                                    ),
                                                }
                                                # Only add if we don't already have this shot_index
                                                if timestamp[
                                                    "start_time"
                                                ] is not None and not any(
                                                    ts["shot_index"]
                                                    == timestamp["shot_index"]
                                                    for ts in video_info["timestamps"]
                                                ):
                                                    video_info["timestamps"].append(
                                                        timestamp
                                                    )
                                    except json.JSONDecodeError:
                                        pass

                                # Look for summary
                                if "summary" in content_text:
                                    try:
                                        summary_start = content_text.find(
                                            '"summary": "'
                                        )
                                        if summary_start >= 0:
                                            summary_start += len('"summary": "')
                                            summary_end = content_text.find(
                                                '"', summary_start
                                            )
                                            if summary_end >= 0:
                                                summary = content_text[
                                                    summary_start:summary_end
                                                ]
                                                if not video_info["summary"]:
                                                    video_info["summary"] = summary
                                    except Exception:
                                        pass

                                # Look for transcript
                                if "[spk_0]" in content_text:
                                    try:
                                        transcript_start = content_text.find("[spk_0]")
                                        transcript_end = content_text.find(
                                            '"', transcript_start
                                        )
                                        if (
                                            transcript_start >= 0
                                            and transcript_end >= 0
                                        ):
                                            transcript = content_text[
                                                transcript_start:transcript_end
                                            ]
                                            if not video_info["transcript"]:
                                                video_info["transcript"] = transcript
                                    except Exception:
                                        pass

                        except Exception as e:
                            print(f"Error processing reference: {e}")
                            continue

        # Sort timestamps by start time
        if video_info["timestamps"]:
            video_info["timestamps"].sort(key=lambda x: x["start_time"])

        return video_info

    except Exception as e:
        print(f"Error parsing response: {e}")
        return video_info


def get_video_from_metadata(bucket, key):
    try:
        # Create S3 client
        s3_client = boto3.client("s3")

        # First get the JSON file from S3
        json_response = s3_client.get_object(Bucket=bucket, Key=key)

        # Read and parse the JSON content
        json_content = json.loads(json_response["Body"].read().decode("utf-8"))

        # Extract the video S3 location from metadata
        if "metadata" in json_content:
            metadata = json_content["metadata"]
            video_bucket = metadata.get("s3_bucket")
            video_key = metadata.get("s3_key")

            if video_bucket and video_key:
                # Generate presigned URL
                video_url = s3_client.generate_presigned_url(
                    "get_object",
                    Params={"Bucket": video_bucket, "Key": video_key},
                    ExpiresIn=3600,  # URL expires in 1 hour
                )

                # Display video directly using the presigned URL
                from IPython.display import display, HTML

                video_player = HTML(
                    f"""
                <video width="800" height="600" controls>
                    <source src="{video_url}" type="video/x-m4v">
                    Your browser does not support the video tag.
                </video>
                """
                )

                display(video_player)
                return True

        print("Could not find video S3 location in metadata")
        return False

    except Exception as e:
        print(f"Error playing video: {str(e)}")
        return False


def play_video_from_bedrock_response(bedrock_response):
    """
    Extract video from Bedrock response and play it in notebook
    Args:
        bedrock_response: Response text containing S3 URI to JSON metadata
    Returns:
        bool: True if video plays successfully, False otherwise
    """
    try:
        s3_client = boto3.client("s3")

        # Extract JSON S3 URI from response
        pattern = r"\[?(s3://[^\s\]]+)\]?"
        match = re.search(pattern, bedrock_response)
        if not match:
            print("No S3 link found in response")
            return False

        # Parse JSON S3 URI
        json_uri = match.group(1)
        json_path = json_uri.replace("s3://", "")
        json_bucket = json_path.split("/")[0]
        json_key = "/".join(json_path.split("/")[1:])

        # Get and parse JSON content
        json_response = s3_client.get_object(Bucket=json_bucket, Key=json_key)
        json_data = json.loads(json_response["Body"].read().decode("utf-8"))

        # Get video location from metadata
        metadata = json_data.get("metadata", {})
        video_bucket = metadata.get("s3_bucket")
        video_key = metadata.get("s3_key")

        if not (video_bucket and video_key):
            print("No video location found in metadata")
            return False

        # Get and display video
        url = s3_client.generate_presigned_url(
            "get_object",
            Params={"Bucket": video_bucket, "Key": video_key},
            ExpiresIn=3600,  # URL valid for 1 hour
        )

        video_player = HTML(
            f"""
        <video width="800" height="600" controls>
            <source src="{url}" type="video/mp4">
            Your browser does not support the video tag.
        </video>
        """
        )

        display(video_player)
        return True

    except Exception as e:
        print(f"Error: {str(e)}")
        return False


__all__ = ["play_video_from_bedrock_response"]


def play_audio_segments_from_s3(audio_s3_info, timestamps):
    """
    Display and play audio segments based on extracted timestamps
    """
    try:
        s3_client = boto3.client("s3")

        # Get the JSON file to read the actual audio file location
        json_response = s3_client.get_object(
            Bucket=audio_s3_info["bucket"], Key=audio_s3_info["key"]
        )
        json_content = json.loads(json_response["Body"].read().decode("utf-8"))

        # Extract the actual audio file information
        if "metadata" in json_content:
            audio_bucket = json_content["metadata"]["s3_bucket"]
            audio_key = json_content["metadata"]["s3_key"]
        else:
            print("Could not find audio file information in JSON metadata")
            return False

        # Generate presigned URL for the audio file
        url = s3_client.generate_presigned_url(
            "get_object",
            Params={"Bucket": audio_bucket, "Key": audio_key},
            ExpiresIn=3600,
        )

        print("\nTranscript Segments:")
        print("-" * 50)

        # Create players for each segment
        for i, ts in enumerate(timestamps, 1):
            start_time = ts["start"] / 1000  # Convert to seconds
            end_time = ts["end"] / 1000  # Convert to seconds

            print(f"\nSegment {i}:")
            print(f"Text: {ts['text']}")
            print(f"Time Range: {start_time:.2f}s - {end_time:.2f}s")

            # Create segment-specific player
            segment_player = HTML(
                f"""
            <div style="margin: 10px 0;">
                <audio controls style="width: 100%" preload="metadata">
                    <source src="{url}#t={start_time},{end_time}" type="audio/wav">
                    Your browser does not support the audio element.
                </audio>
                <div style="font-size: 0.9em; color: #666;">
                    Segment duration: {(end_time - start_time):.2f} seconds
                </div>
            </div>
            """
            )

            display(segment_player)

            print("-" * 50)

        return True

    except Exception as e:
        print(f"Error: {str(e)}")
        return False




def extract_audio_path_and_timestamps_agent_response(response):
    timestamps = []
    audio_s3_info = None

    try:
        for item in response:
            if "audioearningcall_expert" in item:
                source_docs = item["audioearningcall_expert"]["raw_response"][
                    "source_documents"
                ]

                for doc in source_docs:
                    # Extract S3 info from source_metadata
                    if not audio_s3_info:
                        try:
                            source_metadata = doc.metadata["source_metadata"]
                            if "x-amz-bedrock-kb-source-uri" in source_metadata:
                                uri = source_metadata["x-amz-bedrock-kb-source-uri"]
                                if uri.startswith("s3://"):
                                    parts = uri[5:].split("/", 1)
                                    if len(parts) == 2:
                                        audio_s3_info = {
                                            "bucket": parts[0],
                                            "key": parts[1],
                                        }
                        except (AttributeError, KeyError):
                            continue

                    # Extract timestamps
                    if hasattr(doc, "page_content"):
                        content = doc.page_content

                        # Handle the case where content is a single transcript segment
                        if content.strip().startswith(","):
                            content = content.strip()[1:].strip()

                        try:
                            # Try to parse as JSON
                            if content.strip().startswith("{"):
                                json_obj = json.loads(content)

                                # Case 1: Single transcript segment
                                if all(
                                    key in json_obj
                                    for key in [
                                        "start_timestamp_millis",
                                        "end_timestamp_millis",
                                        "segment_index",
                                        "type",
                                        "text",
                                    ]
                                ):
                                    if json_obj["type"] == "TRANSCRIPT":
                                        timestamps.append(
                                            {
                                                "start": int(
                                                    json_obj["start_timestamp_millis"]
                                                ),
                                                "end": int(
                                                    json_obj["end_timestamp_millis"]
                                                ),
                                                "segment_index": int(
                                                    json_obj["segment_index"]
                                                ),
                                                "text": json_obj["text"],
                                                "speaker": "Speaker",
                                            }
                                        )

                                # Case 2: Audio segments array
                                elif "audio_segments" in json_obj:
                                    for segment in json_obj["audio_segments"]:
                                        if segment["type"] == "TRANSCRIPT":
                                            timestamps.append(
                                                {
                                                    "start": int(
                                                        segment[
                                                            "start_timestamp_millis"
                                                        ]
                                                    ),
                                                    "end": int(
                                                        segment["end_timestamp_millis"]
                                                    ),
                                                    "segment_index": int(
                                                        segment["segment_index"]
                                                    ),
                                                    "text": segment["text"],
                                                    "speaker": "Speaker",
                                                }
                                            )

                        except json.JSONDecodeError as e:
                            print(f"JSON decode error: {e}")
                            # Try to extract timestamp information using string operations
                            if (
                                '"start_timestamp_millis"' in content
                                and '"type": "TRANSCRIPT"' in content
                            ):
                                try:
                                    # Extract values using string operations
                                    start_str = content.split(
                                        '"start_timestamp_millis": '
                                    )[1].split(",")[0]
                                    end_str = content.split('"end_timestamp_millis": ')[
                                        1
                                    ].split(",")[0]
                                    index_str = content.split('"segment_index": ')[
                                        1
                                    ].split(",")[0]
                                    text = content.split('"text": "')[1].split('"')[0]

                                    timestamps.append(
                                        {
                                            "start": int(start_str),
                                            "end": int(end_str),
                                            "segment_index": int(index_str),
                                            "text": text,
                                            "speaker": "Speaker",
                                        }
                                    )
                                except Exception as e:
                                    print(f"String parsing error: {e}")
                                    continue

    except Exception as e:
        print(f"Error in main processing: {e}")

    # Sort timestamps and remove duplicates
    if timestamps:
        # Remove duplicates based on segment_index
        unique_timestamps = {t["segment_index"]: t for t in timestamps}.values()
        timestamps = sorted(unique_timestamps, key=lambda x: x["segment_index"])

    return audio_s3_info, timestamps
